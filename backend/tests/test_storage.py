import io
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_LOCAL_STORAGE_DIR, Settings
from app.storage import (
    CHUNK_SIZE,
    InvalidStorageKeyError,
    LocalStorage,
    StorageBackend,
    StorageConfigurationError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    get_storage,
    validate_key,
    video_source_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]

INVALID_KEYS = [
    "",
    "..",
    "../outside.txt",
    "a/../../outside.txt",
    "a/..",
    "a..b",
    "/etc/passwd",
    "a\\b",
    "..\\outside.txt",
    "C:/outside.txt",
    "C:outside.txt",
    "a//b",
    "a/",
    ".",
    "a/./b",
    ".hidden",
    "a/.upload-x.tmp",
    "a b",
    "a\x00b",
    "file.",
    "a./b",
    "NUL",
    "con.txt",
    "users/1/AUX",
    "com1",
    "LPT9.mp4",
    "x" * 1025,
]


class RecordingReader(io.BytesIO):
    """BytesIO that records the size requested by every read call."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.requested: list[int] = []

    def read(self, size=-1):
        self.requested.append(size)
        return super().read(size)


class FailingReader(io.RawIOBase):
    """Stream that returns one chunk and then fails."""

    def __init__(self):
        self.calls = 0

    def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return b"partial"
        raise OSError("connection lost")


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "storage")


def files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()] if path.exists() else []


def test_save_open_size_exists_delete_round_trip(storage):
    key = "users/1/videos/2/source.mp4"
    data = b"video bytes" * 100

    assert storage.exists(key) is False
    assert storage.save(key, io.BytesIO(data)) == len(data)
    assert storage.exists(key) is True
    assert storage.size(key) == len(data)
    with storage.open(key) as stream:
        assert stream.read() == data

    storage.delete(key)

    assert storage.exists(key) is False
    with pytest.raises(StorageObjectNotFoundError):
        storage.size(key)


def test_save_replaces_existing_object(storage):
    storage.save("a/file.bin", io.BytesIO(b"old contents"))
    storage.save("a/file.bin", io.BytesIO(b"new"))

    with storage.open("a/file.bin") as stream:
        assert stream.read() == b"new"
    assert storage.size("a/file.bin") == 3


def test_save_empty_file(storage):
    assert storage.save("empty.bin", io.BytesIO(b"")) == 0
    assert storage.exists("empty.bin")
    assert storage.size("empty.bin") == 0


def test_large_file_is_written_in_bounded_chunks(storage):
    data = os.urandom(CHUNK_SIZE * 2 + CHUNK_SIZE // 2)
    reader = RecordingReader(data)

    assert storage.save("big.bin", reader) == len(data)

    # Never read the whole stream at once, and never more than one chunk.
    assert len(reader.requested) >= 4
    assert all(0 < size <= CHUNK_SIZE for size in reader.requested)
    with storage.open("big.bin") as stream:
        assert stream.read() == data


def test_custom_chunk_size_is_used(tmp_path):
    storage = LocalStorage(tmp_path, chunk_size=4)
    reader = RecordingReader(b"0123456789")

    assert storage.save("small.bin", reader) == 10
    assert reader.requested == [4, 4, 4, 4]


def test_invalid_chunk_size_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        LocalStorage(tmp_path, chunk_size=0)


def test_save_rejects_data_over_max_bytes_and_stores_nothing(storage):
    with pytest.raises(StorageObjectTooLargeError):
        storage.save("a/too-big.bin", io.BytesIO(b"x" * 11), max_bytes=10)

    assert storage.exists("a/too-big.bin") is False
    assert files_under(storage.root) == []


def test_save_accepts_data_exactly_at_max_bytes(storage):
    assert storage.save("a/ok.bin", io.BytesIO(b"x" * 10), max_bytes=10) == 10


def test_failed_save_leaves_no_partial_object_and_keeps_previous(storage):
    storage.save("a/file.bin", io.BytesIO(b"previous"))

    with pytest.raises(OSError, match="connection lost"):
        storage.save("a/file.bin", FailingReader())

    with storage.open("a/file.bin") as stream:
        assert stream.read() == b"previous"
    assert files_under(storage.root) == [storage.root / "a" / "file.bin"]


def test_save_rejects_text_stream(storage):
    with pytest.raises(TypeError):
        storage.save("a/file.txt", io.StringIO("text"))

    assert files_under(storage.root) == []


@pytest.mark.parametrize(
    "key",
    [
        "file.txt",
        "users/1/videos/2/source.mp4",
        "a/b/c/d/e/f.tar.gz",
        "users/con1/nul_x/console.mp4",
        "users/0f8fad5b-d9cb-469f-a165-70867728950e/exports/clip_1-final.mp4",
    ],
)
def test_nested_safe_keys_are_stored_beneath_root(storage, key):
    storage.save(key, io.BytesIO(b"data"))

    path = storage.root.joinpath(*key.split("/"))
    assert path.read_bytes() == b"data"
    assert path.resolve().is_relative_to(storage.root)
    assert storage.exists(key)


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_validate_key_rejects_unsafe_keys(key):
    with pytest.raises(InvalidStorageKeyError):
        validate_key(key)


@pytest.mark.parametrize("key", [None, 1, b"a/b"])
def test_validate_key_rejects_non_string_keys(key):
    with pytest.raises(InvalidStorageKeyError):
        validate_key(key)


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_every_operation_rejects_unsafe_keys(tmp_path, key):
    root = tmp_path / "root"
    storage = LocalStorage(root)
    (tmp_path / "outside.txt").write_bytes(b"secret")

    with pytest.raises(InvalidStorageKeyError):
        storage.save(key, io.BytesIO(b"data"))
    with pytest.raises(InvalidStorageKeyError):
        storage.open(key)
    with pytest.raises(InvalidStorageKeyError):
        storage.exists(key)
    with pytest.raises(InvalidStorageKeyError):
        storage.delete(key)
    with pytest.raises(InvalidStorageKeyError):
        storage.size(key)
    with pytest.raises(InvalidStorageKeyError):
        storage.get_download_url(key, 60)
    with pytest.raises(InvalidStorageKeyError):
        with storage.local_path(key):
            pass

    assert (tmp_path / "outside.txt").read_bytes() == b"secret"
    assert files_under(tmp_path) == [tmp_path / "outside.txt"]


def make_symlink(link: Path, target: Path, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not permitted on this system.")


def test_symlinked_directory_cannot_escape_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    make_symlink(root / "link", outside, target_is_directory=True)
    storage = LocalStorage(root)

    with pytest.raises(InvalidStorageKeyError):
        storage.save("link/new.txt", io.BytesIO(b"data"))
    with pytest.raises(InvalidStorageKeyError):
        storage.open("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.exists("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.size("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.delete("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        with storage.local_path("link/secret.txt"):
            pass

    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_bytes() == b"secret"


def test_symlinked_file_cannot_escape_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"secret")
    make_symlink(root / "file.txt", secret, target_is_directory=False)
    storage = LocalStorage(root)

    with pytest.raises(InvalidStorageKeyError):
        storage.open("file.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.save("file.txt", io.BytesIO(b"overwritten"))
    with pytest.raises(InvalidStorageKeyError):
        storage.delete("file.txt")

    assert secret.read_bytes() == b"secret"


def test_symlinked_root_is_allowed(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    make_symlink(tmp_path / "root-link", real_root, target_is_directory=True)
    storage = LocalStorage(tmp_path / "root-link")

    storage.save("a/file.txt", io.BytesIO(b"data"))

    assert (real_root / "a" / "file.txt").read_bytes() == b"data"


def test_paths_resolving_outside_root_are_rejected_without_real_symlinks(
    tmp_path, monkeypatch
):
    # Simulates root/link being a symlink to an outside directory, so the
    # containment check runs even where creating symlinks is not permitted.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    storage = LocalStorage(root)
    link = storage.root / "link"
    original_resolve = Path.resolve

    def fake_resolve(self, strict=False):
        resolved = original_resolve(self, strict=strict)
        if resolved.is_relative_to(link):
            return outside.joinpath(resolved.relative_to(link))
        return resolved

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with pytest.raises(InvalidStorageKeyError):
        storage.save("link/new.txt", io.BytesIO(b"data"))
    with pytest.raises(InvalidStorageKeyError):
        storage.open("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.exists("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.size("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        storage.delete("link/secret.txt")
    with pytest.raises(InvalidStorageKeyError):
        with storage.local_path("link/secret.txt"):
            pass

    monkeypatch.undo()
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert files_under(root) == []


def test_open_and_size_of_missing_object_raise_not_found(storage):
    with pytest.raises(StorageObjectNotFoundError):
        storage.open("missing.txt")
    with pytest.raises(FileNotFoundError):
        storage.size("missing.txt")
    with pytest.raises(StorageObjectNotFoundError):
        with storage.local_path("missing.txt"):
            pass


def test_delete_missing_object_is_a_no_op(storage):
    storage.delete("missing/file.txt")

    assert storage.exists("missing/file.txt") is False


def test_directory_is_not_an_object(storage):
    storage.save("a/b/file.txt", io.BytesIO(b"data"))

    assert storage.exists("a/b") is False
    with pytest.raises(StorageObjectNotFoundError):
        storage.open("a/b")
    with pytest.raises(InvalidStorageKeyError):
        storage.delete("a/b")
    assert storage.exists("a/b/file.txt")


def test_get_download_url_returns_none_for_local_storage(storage):
    storage.save("a/file.mp4", io.BytesIO(b"data"))

    assert storage.get_download_url("a/file.mp4", 3600) is None


def test_local_path_yields_readable_file(storage):
    storage.save("users/1/videos/2/source.mp4", io.BytesIO(b"frames"))

    with storage.local_path("users/1/videos/2/source.mp4") as path:
        assert isinstance(path, Path)
        assert path.is_file()
        assert path.read_bytes() == b"frames"
        assert path.resolve().is_relative_to(storage.root)


class InMemoryStorage(StorageBackend):
    """Minimal remote-like backend used to test the default local_path."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def save(self, key, fileobj, *, max_bytes=None):
        self.objects[validate_key(key)] = fileobj.read()
        return len(self.objects[key])

    def open(self, key):
        try:
            return io.BytesIO(self.objects[validate_key(key)])
        except KeyError:
            raise StorageObjectNotFoundError(key) from None

    def exists(self, key):
        return validate_key(key) in self.objects

    def delete(self, key):
        self.objects.pop(validate_key(key), None)

    def size(self, key):
        return len(self.objects[validate_key(key)])

    def get_download_url(self, key, expires_seconds):
        return f"https://storage.invalid/{key}"


def test_default_local_path_downloads_to_temp_file_and_cleans_up():
    storage = InMemoryStorage()
    storage.save("users/1/videos/2/source.mp4", io.BytesIO(b"remote frames"))

    with storage.local_path("users/1/videos/2/source.mp4") as path:
        assert path.suffix == ".mp4"
        assert path.read_bytes() == b"remote frames"

    assert not path.exists()


def test_default_local_path_cleans_up_when_object_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    storage = InMemoryStorage()

    with pytest.raises(StorageObjectNotFoundError):
        with storage.local_path("missing.mp4"):
            pass

    assert list(tmp_path.iterdir()) == []


def test_default_local_path_cleans_up_when_body_raises():
    storage = InMemoryStorage()
    storage.save("a.wav", io.BytesIO(b"audio"))

    with pytest.raises(RuntimeError):
        with storage.local_path("a.wav") as path:
            raise RuntimeError("ffmpeg failed")

    assert not path.exists()


def test_video_source_key_format():
    assert video_source_key(1, 42, ".mp4") == "users/1/videos/42/source.mp4"
    assert video_source_key("u-1", "v_2", ".MOV") == "users/u-1/videos/v_2/source.mov"
    assert video_source_key(1, 2) == "users/1/videos/2/source"


@pytest.mark.parametrize(
    ("user_id", "video_id", "ext"),
    [
        ("../1", 2, ".mp4"),
        (1, "a/b", ".mp4"),
        ("", 2, ".mp4"),
        (True, 2, ".mp4"),
        (None, 2, ".mp4"),
        (1, 2, "mp4"),
        (1, 2, "."),
        (1, 2, "./../x"),
        (1, 2, ".mp4/../../x"),
        (1, 2, ".m\\p4"),
        (1, 2, ".toolongextension"),
    ],
)
def test_video_source_key_rejects_invalid_parts(user_id, video_id, ext):
    with pytest.raises(InvalidStorageKeyError):
        video_source_key(user_id, video_id, ext)


def test_get_storage_returns_local_backend_rooted_at_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "files"))

    storage = get_storage()

    assert isinstance(storage, LocalStorage)
    assert storage.root == (tmp_path / "files").resolve()


def test_get_storage_accepts_explicit_settings(tmp_path):
    storage = get_storage(Settings(STORAGE_BACKEND="local", LOCAL_STORAGE_DIR=tmp_path))

    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path.resolve()


def test_get_storage_rejects_unconfigured_s3_backend():
    # S3 behaviour is covered in tests/test_storage_s3.py.
    settings = Settings(
        STORAGE_BACKEND="s3",
        S3_BUCKET=None,
        S3_REGION=None,
        S3_ACCESS_KEY_ID=None,
        S3_SECRET_ACCESS_KEY=None,
    )

    with pytest.raises(StorageConfigurationError, match="s3"):
        get_storage(settings)


def test_local_storage_dir_defaults_to_backend_storage(monkeypatch):
    monkeypatch.delenv("LOCAL_STORAGE_DIR", raising=False)

    assert Settings().LOCAL_STORAGE_DIR == DEFAULT_LOCAL_STORAGE_DIR
    assert DEFAULT_LOCAL_STORAGE_DIR == REPO_ROOT / "backend" / "storage"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_local_storage_dir_is_rejected(monkeypatch, value):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", value)

    with pytest.raises(ValidationError, match="LOCAL_STORAGE_DIR"):
        Settings()


def test_local_storage_dir_is_git_ignored():
    # The rule lives in backend/.gitignore so it travels with the backend tree.
    patterns = (BACKEND_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/storage/" in patterns
    assert DEFAULT_LOCAL_STORAGE_DIR == BACKEND_DIR / "storage"
