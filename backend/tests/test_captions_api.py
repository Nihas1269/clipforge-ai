"""Caption and caption style endpoints."""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.core.security import create_access_token
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.caption import CaptionStyle, ClipCaption
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.transcript import Transcript, TranscriptSegment
from app.models.user import User
from app.models.video import Video, VideoStatus
from app.repositories.clips import replace_suggestions
from app.services.video_deletion import delete_video
from app.storage import LocalStorage

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"

# The clip covers 10-40 s of the video, so caption times are 0-30 s.
CLIP_START = 10.0
CLIP_END = 40.0


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(session_factory, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("CAPTION_FONTS", raising=False)
    get_settings.cache_clear()
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db
    with TestClient(application) as test_client:
        yield test_client


def make_user(db, email: str) -> User:
    user = User(email=email, hashed_password="not-a-real-hash")
    db.add(user)
    db.commit()
    return user


def auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user.id, token_version=user.token_version, settings=get_settings()
    )
    return {"Authorization": f"Bearer {token}"}


def make_clip(db, user: User, *, with_transcript: bool = True) -> Clip:
    video = Video(
        user_id=user.id,
        original_filename="talk.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        storage_key=f"users/{user.id}/videos/x/source.mp4",
        status=VideoStatus.READY,
        duration_seconds=120.0,
    )
    db.add(video)
    db.flush()
    clip = Clip(
        video_id=video.id,
        start_seconds=CLIP_START,
        end_seconds=CLIP_END,
        title="A clip",
        reason="Test.",
        score=0.5,
        status=ClipStatus.SUGGESTED,
        source=ClipSource.HEURISTIC,
    )
    db.add(clip)
    if with_transcript:
        transcript = Transcript(video_id=video.id, provider="fake", full_text="...")
        db.add(transcript)
        db.flush()
        segments = [
            # Before the clip.
            (0, 2.0, 5.0, "too early", [("too", 2.0, 2.5), ("early", 2.5, 3.0)]),
            (
                1,
                9.0,
                14.0,
                "so here is the most important lesson",
                [
                    ("so", 9.0, 9.4),
                    ("here", 10.0, 10.4),
                    ("is", 10.4, 10.6),
                    ("the", 10.6, 10.8),
                    ("most", 10.8, 11.2),
                    ("important", 11.2, 12.0),
                    ("lesson", 12.0, 12.6),
                ],
            ),
            # No word timings: segment-level fallback.
            (2, 20.0, 24.0, "Segment level only.", None),
            (3, 38.0, 45.0, "ends after the clip", [("ends", 38.0, 39.0)]),
        ]
        for index, start, end, text, words in segments:
            db.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    index=index,
                    start_seconds=start,
                    end_seconds=end,
                    text=text,
                    words=(
                        None
                        if words is None
                        else [{"word": w, "start": s, "end": e} for w, s, e in words]
                    ),
                )
            )
    db.commit()
    return clip


@pytest.fixture
def owner(db):
    return make_user(db, "owner@example.com")


@pytest.fixture
def other(db):
    return make_user(db, "other@example.com")


@pytest.fixture
def clip(db, owner):
    return make_clip(db, owner)


def captions_url(clip_id) -> str:
    return f"/api/v1/clips/{clip_id}/captions"


def style_url(clip_id) -> str:
    return f"/api/v1/clips/{clip_id}/caption-style"


def caption_count(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(ClipCaption))


def error_code(response) -> str:
    return response.json()["error"]["code"]


VALID_CAPTIONS = {
    "captions": [
        {"start_seconds": 0.0, "end_seconds": 1.5, "text": "Hello there"},
        {"start_seconds": 1.5, "end_seconds": 3.0, "text": "General\nKenobi"},
    ]
}


# Authentication and ownership


ENDPOINTS = [
    ("get", "/captions", None),
    ("post", "/captions/generate", None),
    ("put", "/captions", VALID_CAPTIONS),
    ("get", "/caption-style", None),
    ("put", "/caption-style", {"preset": "bold"}),
]


@pytest.mark.parametrize("method, suffix, body", ENDPOINTS)
def test_endpoints_require_authentication(client, clip, method, suffix, body):
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, f"/api/v1/clips/{clip.id}{suffix}", **kwargs)

    assert response.status_code == 401


@pytest.mark.parametrize("method, suffix, body", ENDPOINTS)
def test_other_users_cannot_read_or_change_captions(
    client, db, clip, other, method, suffix, body
):
    kwargs = {"json": body} if body is not None else {}
    response = client.request(
        method, f"/api/v1/clips/{clip.id}{suffix}", headers=auth(other), **kwargs
    )

    assert response.status_code == 404
    assert error_code(response) == "not_found"
    assert caption_count(db) == 0
    assert db.scalar(select(func.count()).select_from(CaptionStyle)) == 0


@pytest.mark.parametrize("method, suffix, body", ENDPOINTS)
def test_unknown_clip_is_not_found(client, owner, method, suffix, body):
    kwargs = {"json": body} if body is not None else {}
    response = client.request(
        method, f"/api/v1/clips/{uuid.uuid4()}{suffix}", headers=auth(owner), **kwargs
    )

    assert response.status_code == 404


# Generation


def test_generate_builds_clip_relative_captions(client, clip, owner, caplog):
    caplog.set_level(logging.INFO, logger="app.api.v1.captions")

    response = client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    assert response.status_code == 200
    body = response.json()
    assert body["clip_id"] == str(clip.id)
    items = body["items"]
    assert body["total"] == len(items)
    assert [item["index"] for item in items] == list(range(len(items)))
    simplified = [(i["start_seconds"], i["end_seconds"], i["text"]) for i in items]
    # Default style: classic, 32 characters per line.
    assert simplified == [
        (0.0, 2.0, "here is the most important"),
        (2.0, 2.6, "lesson"),
        (10.0, 14.0, "Segment level only."),
        (28.0, 29.0, "ends"),
    ]
    for item in items:
        assert 0 <= item["start_seconds"] < item["end_seconds"] <= CLIP_END - CLIP_START
        assert all(len(line) <= 32 for line in item["text"].split("\n"))

    response = client.get(captions_url(clip.id), headers=auth(owner))
    assert response.json()["items"] == items

    record = next(r for r in caplog.records if r.message == "captions_generated")
    assert record.caption_count == 4
    # No caption text in the logs.
    assert "lesson" not in caplog.text


def test_generate_uses_the_style_line_length(client, clip, owner):
    response = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "classic", "max_chars_per_line": 10},
    )
    assert response.status_code == 200

    response = client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    texts = [item["text"] for item in response.json()["items"]]
    assert texts[:4] == ["here is", "the most", "important", "lesson"]
    for text in texts:
        assert all(len(line) <= 10 for line in text.split("\n"))
    # The segment-level caption is wrapped, not split into invented timings.
    assert "Segment\nlevel\nonly." in texts


def test_generate_replaces_user_edits(client, db, clip, owner):
    client.put(captions_url(clip.id), headers=auth(owner), json=VALID_CAPTIONS)

    response = client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))
    again = client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    texts = [item["text"] for item in response.json()["items"]]
    assert "Hello there" not in texts
    # Regenerating does not duplicate captions.
    assert again.json()["items"] == [
        {**item, "id": again_item["id"]}
        for item, again_item in zip(response.json()["items"], again.json()["items"])
    ]
    assert caption_count(db) == len(texts)


def test_generate_without_transcript_is_conflict(client, db, owner):
    clip = make_clip(db, owner, with_transcript=False)

    response = client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    assert response.status_code == 409
    assert error_code(response) == "transcript_required"


def test_list_is_empty_before_generation(client, clip, owner):
    response = client.get(captions_url(clip.id), headers=auth(owner))

    assert response.status_code == 200
    assert response.json() == {"clip_id": str(clip.id), "items": [], "total": 0}


# Editing


def test_put_replaces_captions(client, db, clip, owner):
    client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    response = client.put(
        captions_url(clip.id),
        headers=auth(owner),
        json={
            "captions": [
                {"start_seconds": 0, "end_seconds": 1.5, "text": "  Hello\x00 there "},
                {"start_seconds": 1.5, "end_seconds": 30.0004, "text": "A\r\n\r\nB"},
            ]
        },
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [
        (i["index"], i["start_seconds"], i["end_seconds"], i["text"]) for i in items
    ] == [
        (0, 0.0, 1.5, "Hello there"),
        # Within the 1 ms tolerance: clamped to the clip end.
        (1, 1.5, 30.0, "A\nB"),
    ]
    assert caption_count(db) == 2


def test_put_empty_list_clears_captions(client, db, clip, owner):
    client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))

    response = client.put(
        captions_url(clip.id), headers=auth(owner), json={"captions": []}
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert caption_count(db) == 0


@pytest.mark.parametrize(
    "captions, code",
    [
        # Past the end of the 30-second clip.
        (
            [{"start_seconds": 29.0, "end_seconds": 30.5, "text": "x"}],
            "caption_outside_clip",
        ),
        (
            [{"start_seconds": 30.0, "end_seconds": 31.0, "text": "x"}],
            "caption_outside_clip",
        ),
        # Overlapping.
        (
            [
                {"start_seconds": 0.0, "end_seconds": 2.0, "text": "a"},
                {"start_seconds": 1.9, "end_seconds": 3.0, "text": "b"},
            ],
            "captions_overlap",
        ),
        # Out of order.
        (
            [
                {"start_seconds": 5.0, "end_seconds": 6.0, "text": "a"},
                {"start_seconds": 1.0, "end_seconds": 2.0, "text": "b"},
            ],
            "captions_overlap",
        ),
        # Collapses to an empty range at millisecond precision.
        (
            [{"start_seconds": 1.0001, "end_seconds": 1.0003, "text": "x"}],
            "invalid_caption_timing",
        ),
        # Longer than the default 32 characters per line.
        (
            [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "x" * 33}],
            "caption_line_too_long",
        ),
    ],
)
def test_put_rejects_invalid_captions(client, db, clip, owner, captions, code):
    client.put(captions_url(clip.id), headers=auth(owner), json=VALID_CAPTIONS)

    response = client.put(
        captions_url(clip.id), headers=auth(owner), json={"captions": captions}
    )

    assert response.status_code == 422
    assert error_code(response) == code
    # Nothing was changed.
    assert caption_count(db) == 2


@pytest.mark.parametrize(
    "caption",
    [
        {"start_seconds": 2.0, "end_seconds": 2.0, "text": "x"},
        {"start_seconds": 3.0, "end_seconds": 2.0, "text": "x"},
        {"start_seconds": -1.0, "end_seconds": 2.0, "text": "x"},
        {"start_seconds": "1", "end_seconds": 2.0, "text": "x"},
        {"start_seconds": 0.0, "end_seconds": 2.0, "text": " \n "},
        {"start_seconds": 0.0, "end_seconds": 2.0},
        {"start_seconds": 0.0, "end_seconds": 2.0, "text": "x", "style": "bold"},
    ],
)
def test_put_rejects_malformed_captions(client, clip, owner, caption):
    response = client.put(
        captions_url(clip.id), headers=auth(owner), json={"captions": [caption]}
    )

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_put_rejects_too_many_captions(client, clip, owner):
    captions = [
        {"start_seconds": i * 0.01, "end_seconds": i * 0.01 + 0.01, "text": "x"}
        for i in range(601)
    ]

    response = client.put(
        captions_url(clip.id), headers=auth(owner), json={"captions": captions}
    )

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_put_line_length_follows_the_style(client, clip, owner):
    client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "classic", "max_chars_per_line": 60},
    )

    response = client.put(
        captions_url(clip.id),
        headers=auth(owner),
        json={"captions": [{"start_seconds": 0, "end_seconds": 1, "text": "x" * 60}]},
    )

    assert response.status_code == 200


def test_deleting_a_clip_deletes_its_captions_and_style(client, db, clip, owner):
    client.post(f"{captions_url(clip.id)}/generate", headers=auth(owner))
    client.put(style_url(clip.id), headers=auth(owner), json={"preset": "bold"})

    response = client.delete(f"/api/v1/clips/{clip.id}", headers=auth(owner))

    assert response.status_code == 204
    assert caption_count(db) == 0
    assert db.scalar(select(func.count()).select_from(CaptionStyle)) == 0


def test_replacing_suggestions_deletes_their_captions_and_style(
    client, db, clip, owner
):
    accepted = Clip(
        video_id=clip.video_id,
        start_seconds=50.0,
        end_seconds=80.0,
        title="Kept",
        reason="Test.",
        score=0.5,
        status=ClipStatus.ACCEPTED,
        source=ClipSource.HEURISTIC,
    )
    db.add(accepted)
    db.commit()
    for clip_id in (clip.id, accepted.id):
        client.put(captions_url(clip_id), headers=auth(owner), json=VALID_CAPTIONS)
        client.put(style_url(clip_id), headers=auth(owner), json={"preset": "bold"})

    replace_suggestions(db, clip.video_id, [], source=ClipSource.HEURISTIC)
    db.commit()

    db.expire_all()
    # Only the accepted clip's captions and style are left.
    assert set(db.scalars(select(ClipCaption.clip_id))) == {accepted.id}
    assert list(db.scalars(select(CaptionStyle.clip_id))) == [accepted.id]
    assert caption_count(db) == 2


def test_deleting_a_video_deletes_its_captions_and_style(
    client, db, clip, owner, tmp_path
):
    kept = make_clip(db, owner)
    for clip_id in (clip.id, kept.id):
        client.put(captions_url(clip_id), headers=auth(owner), json=VALID_CAPTIONS)
        client.put(style_url(clip_id), headers=auth(owner), json={"preset": "bold"})

    delete_video(db, LocalStorage(tmp_path), clip.video_id, owner.id)

    db.expire_all()
    # Captions of another video's clip are untouched.
    assert set(db.scalars(select(ClipCaption.clip_id))) == {kept.id}
    assert list(db.scalars(select(CaptionStyle.clip_id))) == [kept.id]
    assert caption_count(db) == 2


# Style


def test_default_style(client, clip, owner):
    response = client.get(style_url(clip.id), headers=auth(owner))

    assert response.status_code == 200
    assert response.json() == {
        "clip_id": str(clip.id),
        "preset": "classic",
        "font_family": "Arial",
        "font_size": 64,
        "primary_color": "#FFFFFFFF",
        "outline_color": "#000000FF",
        "position": "bottom",
        "uppercase": False,
        "max_chars_per_line": 32,
        "is_default": True,
        "allowed_fonts": [
            "Arial",
            "Roboto",
            "Open Sans",
            "Montserrat",
            "Inter",
            "Bebas Neue",
        ],
    }


def test_put_style_stores_values_and_preset_defaults(client, db, clip, owner, caplog):
    caplog.set_level(logging.INFO, logger="app.api.v1.captions")

    response = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={
            "preset": "karaoke",
            "font_family": "open sans",
            "font_size": 90,
            "primary_color": "#00ff00",
            "position": "top",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_default"] is False
    assert body["font_family"] == "Open Sans"
    assert body["font_size"] == 90
    assert body["primary_color"] == "#00FF00FF"
    assert body["position"] == "top"
    # Preset defaults for the omitted fields.
    assert body["outline_color"] == "#000000FF"
    assert body["uppercase"] is True
    assert body["max_chars_per_line"] == 24
    assert client.get(style_url(clip.id), headers=auth(owner)).json() == body

    # A second PUT updates the same row and resets omitted fields.
    response = client.put(
        style_url(clip.id), headers=auth(owner), json={"preset": "minimal"}
    )
    assert response.json()["font_family"] == "Arial"
    assert response.json()["font_size"] == 52
    assert db.scalar(select(func.count()).select_from(CaptionStyle)) == 1
    assert any(r.message == "caption_style_updated" for r in caplog.records)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"preset": "fancy"},
        {"preset": "classic", "position": "left"},
        {"preset": "classic", "primary_color": "red"},
        {"preset": "classic", "primary_color": "#FFF"},
        {"preset": "classic", "outline_color": "rgb(0,0,0)"},
        {"preset": "classic", "font_size": 15},
        {"preset": "classic", "font_size": 161},
        {"preset": "classic", "font_size": 40.5},
        {"preset": "classic", "max_chars_per_line": 9},
        {"preset": "classic", "max_chars_per_line": 81},
        {"preset": "classic", "uppercase": "yes"},
        {"preset": "classic", "font_family": "x" * 65},
        {"preset": "classic", "shadow": 3},
    ],
)
def test_put_style_rejects_invalid_values(client, db, clip, owner, body):
    response = client.put(style_url(clip.id), headers=auth(owner), json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert db.scalar(select(func.count()).select_from(CaptionStyle)) == 0


@pytest.mark.parametrize("font", ["Comic Sans", "Arial,Bold", "Arial{\\b1}"])
def test_put_style_rejects_fonts_outside_the_allowlist(client, clip, owner, font):
    response = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "classic", "font_family": font},
    )

    assert response.status_code == 422
    assert error_code(response) == "font_not_allowed"
    assert "Arial" in response.json()["error"]["details"]["allowed_fonts"]


def test_put_style_rejects_transparent_text(client, clip, owner):
    response = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "classic", "primary_color": "#FFFFFF10"},
    )

    assert response.status_code == 422
    assert error_code(response) == "caption_color_too_transparent"


def test_put_style_commit_failure_is_reported_as_a_style_error(
    client, db, clip, owner, monkeypatch
):
    original_commit = Session.commit

    def failing_commit(self):
        if any(isinstance(item, CaptionStyle) for item in self.new):
            raise RuntimeError("simulated database failure")
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)

    response = client.put(
        style_url(clip.id), headers=auth(owner), json={"preset": "bold"}
    )

    assert response.status_code == 500
    assert error_code(response) == "caption_style_update_failed"
    assert (
        response.json()["error"]["message"] == "The caption style could not be saved."
    )
    assert db.scalar(select(func.count()).select_from(CaptionStyle)) == 0


def test_font_allowlist_comes_from_settings(client, clip, owner, monkeypatch):
    monkeypatch.setenv("CAPTION_FONTS", "Roboto,Lato")
    get_settings.cache_clear()

    default = client.get(style_url(clip.id), headers=auth(owner)).json()
    allowed = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "bold", "font_family": "lato"},
    )
    rejected = client.put(
        style_url(clip.id),
        headers=auth(owner),
        json={"preset": "bold", "font_family": "Arial"},
    )

    assert default["font_family"] == "Roboto"
    assert default["allowed_fonts"] == ["Roboto", "Lato"]
    assert allowed.json()["font_family"] == "Lato"
    assert rejected.status_code == 422
