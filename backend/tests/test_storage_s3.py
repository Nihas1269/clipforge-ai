"""Tests for the S3-compatible storage backend.

No request ever leaves the process: operations run against a real botocore
client with botocore.stub.Stubber attached, or against a mocked client.
"""

import io
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.core.config import Settings
from app.storage import (
    InvalidStorageKeyError,
    S3Storage,
    StorageConfigurationError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    get_storage,
)
from app.storage.s3 import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_PRESIGNED_URL_EXPIRY_SECONDS,
    READ_TIMEOUT_SECONDS,
    TRANSFER_CONFIG,
    create_s3_client,
    is_not_found_error,
)

BUCKET = "test-bucket"

# A representative subset of the unsafe keys covered in tests/test_storage.py.
INVALID_KEYS = [
    "",
    "..",
    "../outside.txt",
    "a/../../outside.txt",
    "/etc/passwd",
    "a\\b",
    "C:/outside.txt",
    "a//b",
    "a/",
    ".hidden",
    "a b",
    "a\x00b",
    "file.",
    "NUL",
    "x" * 1025,
    None,
]
KEY = "users/1/videos/2/source.mp4"
# Obviously fake values; the clients built from them never send a request.
FAKE_ACCESS_KEY_ID = "fake-access-key-id"
FAKE_SECRET = "fake-secret-for-tests"

S3_SETTINGS = {
    "STORAGE_BACKEND": "s3",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_REGION": "us-east-1",
    "S3_BUCKET": BUCKET,
    "S3_ACCESS_KEY_ID": FAKE_ACCESS_KEY_ID,
    "S3_SECRET_ACCESS_KEY": FAKE_SECRET,
    "S3_ADDRESSING_STYLE": "path",
}


@pytest.fixture(autouse=True)
def isolated_aws_environment(monkeypatch, tmp_path):
    # A developer's AWS profile, config or credentials must never influence the
    # tests.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-aws-config"))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-aws-credentials")
    )
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_S3",
    ):
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides) -> Settings:
    return Settings(**{**S3_SETTINGS, **overrides})


def client_error(code: str, status: int, operation: str = "HeadObject") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "error"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


@pytest.fixture
def stubbed():
    """An S3Storage on a real client whose requests are answered by a Stubber."""
    client = create_s3_client(make_settings())
    with Stubber(client) as stubber:
        yield S3Storage(client, BUCKET), stubber
        stubber.assert_no_pending_responses()


@pytest.fixture
def mocked():
    """An S3Storage on a mocked client."""
    client = MagicMock()
    return S3Storage(client, BUCKET), client


# --- configuration --------------------------------------------------------


def test_get_storage_returns_configured_s3_backend():
    storage = get_storage(make_settings(S3_REGION="auto"))

    assert isinstance(storage, S3Storage)
    assert storage.bucket == BUCKET
    meta = storage.client.meta
    assert meta.endpoint_url == "http://localhost:9000"
    assert meta.region_name == "auto"
    assert meta.config.s3 == {"addressing_style": "path"}
    assert meta.config.connect_timeout == CONNECT_TIMEOUT_SECONDS
    assert meta.config.read_timeout == READ_TIMEOUT_SECONDS
    # botocore stores the total attempt count, including the first request.
    assert meta.config.retries["total_max_attempts"] == MAX_ATTEMPTS
    assert "max_attempts" not in meta.config.retries


def test_get_storage_reads_s3_settings_from_environment(monkeypatch):
    for name, value in S3_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("S3_ENDPOINT_URL")

    storage = get_storage()

    assert isinstance(storage, S3Storage)
    assert storage.bucket == BUCKET
    # Without an endpoint URL the client targets AWS S3.
    assert "amazonaws.com" in storage.client.meta.endpoint_url


@pytest.mark.parametrize(
    "name", ["S3_BUCKET", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"]
)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_required_setting_raises_configuration_error(name, value):
    with pytest.raises(StorageConfigurationError, match=name) as exc_info:
        get_storage(make_settings(**{name: value}))

    assert FAKE_SECRET not in str(exc_info.value)


def test_all_missing_settings_are_listed():
    settings = Settings(
        STORAGE_BACKEND="s3",
        S3_BUCKET=None,
        S3_REGION=None,
        S3_ACCESS_KEY_ID=None,
        S3_SECRET_ACCESS_KEY=None,
    )

    with pytest.raises(StorageConfigurationError) as exc_info:
        get_storage(settings)

    message = str(exc_info.value)
    for name in ("S3_BUCKET", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        assert name in message


def test_endpoint_url_without_scheme_is_rejected():
    with pytest.raises(StorageConfigurationError, match="S3_ENDPOINT_URL"):
        get_storage(make_settings(S3_ENDPOINT_URL="localhost:9000"))


def test_s3_settings_are_optional_unless_s3_is_selected(monkeypatch):
    for name in S3_SETTINGS:
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.STORAGE_BACKEND == "local"
    assert settings.S3_ENDPOINT_URL is None
    assert settings.S3_BUCKET is None
    assert settings.S3_SECRET_ACCESS_KEY is None
    assert settings.S3_ADDRESSING_STYLE == "auto"


def test_s3_secret_is_not_shown_in_settings_repr():
    assert FAKE_SECRET not in repr(make_settings())


@pytest.mark.parametrize(
    ("value", "expected"), [("PATH", "path"), (" virtual ", "virtual"), ("", "auto")]
)
def test_addressing_style_is_normalized(value, expected):
    assert make_settings(S3_ADDRESSING_STYLE=value).S3_ADDRESSING_STYLE == expected


def test_invalid_addressing_style_is_rejected():
    with pytest.raises(ValueError):
        make_settings(S3_ADDRESSING_STYLE="sideways")


def test_empty_bucket_is_rejected():
    with pytest.raises(StorageConfigurationError):
        S3Storage(MagicMock(), "")


# --- save -----------------------------------------------------------------


def fake_upload(store: dict, read_size: int = 4):
    """Return an upload_fileobj replacement that reads the stream in chunks."""

    def upload_fileobj(fileobj, bucket, key, Config=None):
        data = b""
        while chunk := fileobj.read(read_size):
            data += chunk
        store[(bucket, key)] = data

    return upload_fileobj


def test_save_uploads_to_configured_bucket_and_key(mocked):
    storage, client = mocked
    store = {}
    client.upload_fileobj.side_effect = fake_upload(store)

    assert storage.save(KEY, io.BytesIO(b"video bytes")) == 11

    assert store == {(BUCKET, KEY): b"video bytes"}
    args, kwargs = client.upload_fileobj.call_args
    assert args[1:] == (BUCKET, KEY)
    assert kwargs["Config"] is TRANSFER_CONFIG


def test_save_empty_object(mocked):
    storage, client = mocked
    store = {}
    client.upload_fileobj.side_effect = fake_upload(store)

    assert storage.save("empty.bin", io.BytesIO(b"")) == 0
    assert store == {(BUCKET, "empty.bin"): b""}


def test_save_over_max_bytes_raises_and_stores_nothing(mocked):
    storage, client = mocked
    store = {}
    client.upload_fileobj.side_effect = fake_upload(store)

    with pytest.raises(StorageObjectTooLargeError):
        storage.save(KEY, io.BytesIO(b"x" * 11), max_bytes=10)

    assert store == {}


def test_save_exactly_max_bytes_succeeds(mocked):
    storage, client = mocked
    store = {}
    client.upload_fileobj.side_effect = fake_upload(store)

    assert storage.save(KEY, io.BytesIO(b"x" * 10), max_bytes=10) == 10


def test_save_rejects_text_stream(mocked):
    storage, client = mocked
    store = {}
    client.upload_fileobj.side_effect = fake_upload(store)

    with pytest.raises(TypeError):
        storage.save(KEY, io.StringIO("text"))

    assert store == {}


@pytest.mark.parametrize(
    "error",
    [
        client_error("AccessDenied", 403, "PutObject"),
        client_error("InternalError", 500, "PutObject"),
        EndpointConnectionError(endpoint_url="http://localhost:9000"),
    ],
)
def test_save_failure_is_propagated(mocked, caplog, error):
    caplog.set_level(logging.INFO, logger="app.storage.s3")
    storage, client = mocked
    client.upload_fileobj.side_effect = error

    with pytest.raises(type(error)):
        storage.save(KEY, io.BytesIO(b"data"))

    assert "storage.saved" not in caplog.messages


def test_save_logs_key_and_size_without_content(mocked, caplog):
    caplog.set_level(logging.INFO, logger="app.storage.s3")
    storage, client = mocked
    client.upload_fileobj.side_effect = fake_upload({})

    storage.save(KEY, io.BytesIO(b"private content"))

    [record] = [r for r in caplog.records if r.getMessage() == "storage.saved"]
    assert record.storage_key == KEY
    assert record.size_bytes == 15
    assert "private content" not in caplog.text


# --- exists / size / open -------------------------------------------------


def test_exists_returns_true_for_existing_object(stubbed):
    storage, stubber = stubbed
    stubber.add_response(
        "head_object", {"ContentLength": 4}, {"Bucket": BUCKET, "Key": KEY}
    )

    assert storage.exists(KEY) is True


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_exists_returns_false_for_confirmed_missing_object(stubbed, code):
    storage, stubber = stubbed
    stubber.add_client_error(
        "head_object",
        service_error_code=code,
        http_status_code=404,
        expected_params={"Bucket": BUCKET, "Key": KEY},
    )

    assert storage.exists(KEY) is False


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("403", 403),
        ("AccessDenied", 403),
        ("InvalidAccessKeyId", 403),
        ("SignatureDoesNotMatch", 403),
        ("NoSuchBucket", 404),
        ("500", 500),
        ("InternalError", 500),
        ("503", 503),
        ("SlowDown", 503),
    ],
)
def test_exists_propagates_errors_other_than_not_found(stubbed, code, status):
    storage, stubber = stubbed
    stubber.add_client_error(
        "head_object",
        service_error_code=code,
        http_status_code=status,
        expected_params={"Bucket": BUCKET, "Key": KEY},
    )

    with pytest.raises(ClientError):
        storage.exists(KEY)


@pytest.mark.parametrize(
    "error",
    [
        ConnectTimeoutError(endpoint_url="http://localhost:9000"),
        ReadTimeoutError(endpoint_url="http://localhost:9000"),
        EndpointConnectionError(endpoint_url="http://localhost:9000"),
    ],
)
def test_exists_propagates_timeouts_and_connection_errors(mocked, error):
    storage, client = mocked
    client.head_object.side_effect = error

    with pytest.raises(type(error)):
        storage.exists(KEY)


def test_size_returns_content_length(stubbed):
    storage, stubber = stubbed
    stubber.add_response(
        "head_object", {"ContentLength": 1234}, {"Bucket": BUCKET, "Key": KEY}
    )

    assert storage.size(KEY) == 1234


def test_size_of_missing_object_raises_not_found(stubbed):
    storage, stubber = stubbed
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

    with pytest.raises(StorageObjectNotFoundError):
        storage.size(KEY)


def test_size_propagates_permission_error(stubbed):
    storage, stubber = stubbed
    stubber.add_client_error("head_object", service_error_code="403", http_status_code=403)

    with pytest.raises(ClientError):
        storage.size(KEY)


def test_open_returns_object_body(stubbed):
    storage, stubber = stubbed
    data = b"remote frames"
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)},
        {"Bucket": BUCKET, "Key": KEY},
    )

    stream = storage.open(KEY)
    try:
        assert stream.read() == data
    finally:
        stream.close()


def test_open_missing_object_raises_not_found(stubbed):
    storage, stubber = stubbed
    stubber.add_client_error(
        "get_object",
        service_error_code="NoSuchKey",
        http_status_code=404,
        expected_params={"Bucket": BUCKET, "Key": KEY},
    )

    with pytest.raises(StorageObjectNotFoundError):
        storage.open(KEY)


def test_open_propagates_permission_error(stubbed):
    storage, stubber = stubbed
    stubber.add_client_error(
        "get_object", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(ClientError):
        storage.open(KEY)


# --- delete ---------------------------------------------------------------


def test_delete_uses_configured_bucket_and_key(stubbed):
    storage, stubber = stubbed
    stubber.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": KEY})

    storage.delete(KEY)


def test_delete_propagates_permission_error(stubbed):
    storage, stubber = stubbed
    stubber.add_client_error(
        "delete_object",
        service_error_code="AccessDenied",
        http_status_code=403,
        expected_params={"Bucket": BUCKET, "Key": KEY},
    )

    with pytest.raises(ClientError):
        storage.delete(KEY)


# --- presigned URLs -------------------------------------------------------


def test_get_download_url_uses_configured_expiry(mocked):
    storage, client = mocked
    client.generate_presigned_url.return_value = "https://example.invalid/signed"

    assert storage.get_download_url(KEY, 900) == "https://example.invalid/signed"

    client.generate_presigned_url.assert_called_once_with(
        "get_object", Params={"Bucket": BUCKET, "Key": KEY}, ExpiresIn=900
    )


def test_get_download_url_signs_offline_with_real_client():
    storage = get_storage(make_settings())

    url = storage.get_download_url(KEY, 900)

    assert url.startswith(f"http://localhost:9000/{BUCKET}/{KEY}?")
    assert "X-Amz-Expires=900" in url
    assert "X-Amz-Signature=" in url
    assert FAKE_SECRET not in url


@pytest.mark.parametrize(
    "expires", [0, -1, MAX_PRESIGNED_URL_EXPIRY_SECONDS + 1, 1.5, "60", True, None]
)
def test_get_download_url_rejects_invalid_expiry(mocked, expires):
    storage, client = mocked

    with pytest.raises(ValueError):
        storage.get_download_url(KEY, expires)

    client.generate_presigned_url.assert_not_called()


def test_get_download_url_accepts_maximum_expiry(mocked):
    storage, client = mocked

    storage.get_download_url(KEY, MAX_PRESIGNED_URL_EXPIRY_SECONDS)

    assert (
        client.generate_presigned_url.call_args.kwargs["ExpiresIn"]
        == MAX_PRESIGNED_URL_EXPIRY_SECONDS
    )


# --- local_path -----------------------------------------------------------


def test_local_path_downloads_to_temp_file_and_cleans_up(mocked):
    storage, client = mocked

    def download_fileobj(bucket, key, fileobj, Config=None):
        assert (bucket, key) == (BUCKET, KEY)
        fileobj.write(b"remote frames")

    client.download_fileobj.side_effect = download_fileobj

    with storage.local_path(KEY) as path:
        assert path.suffix == ".mp4"
        assert path.read_bytes() == b"remote frames"

    assert not path.exists()


def test_local_path_cleans_up_when_body_raises(mocked):
    storage, client = mocked
    client.download_fileobj.side_effect = lambda b, k, f, Config=None: f.write(b"x")

    with pytest.raises(RuntimeError):
        with storage.local_path(KEY) as path:
            raise RuntimeError("ffmpeg failed")

    assert not path.exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client_error("404", 404), StorageObjectNotFoundError),
        (client_error("403", 403), ClientError),
        (client_error("InternalError", 500, "GetObject"), ClientError),
        (ReadTimeoutError(endpoint_url="http://localhost:9000"), ReadTimeoutError),
    ],
)
def test_local_path_failure_raises_and_cleans_up(
    mocked, tmp_path, monkeypatch, error, expected
):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    storage, client = mocked
    client.download_fileobj.side_effect = error

    with pytest.raises(expected):
        with storage.local_path(KEY):
            pytest.fail("the body must not run when the download fails")

    assert list(tmp_path.iterdir()) == []


# --- keys -----------------------------------------------------------------


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_every_operation_rejects_unsafe_keys_without_calling_s3(key):
    client = MagicMock()
    storage = S3Storage(client, BUCKET)

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

    assert client.method_calls == []


def test_is_not_found_error_requires_404_status_and_code():
    assert is_not_found_error(client_error("404", 404))
    assert is_not_found_error(client_error("NoSuchKey", 404))
    assert not is_not_found_error(client_error("NoSuchBucket", 404))
    assert not is_not_found_error(client_error("404", 500))
    assert not is_not_found_error(client_error("AccessDenied", 403))
