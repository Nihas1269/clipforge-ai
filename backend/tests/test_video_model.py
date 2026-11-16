import itertools
import logging
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.database import Base, create_db_engine
from app.models.user import User
from app.models.video import (
    ALLOWED_TRANSITIONS,
    DEFAULT_FAILURE_MESSAGE,
    InvalidStatusTransitionError,
    Video,
    VideoStatus,
    transition_status,
)
from app.schemas.video import VideoList, VideoRead

UPLOADED = VideoStatus.UPLOADED
PROCESSING = VideoStatus.PROCESSING
READY = VideoStatus.READY
FAILED = VideoStatus.FAILED

# Written out explicitly so the tests check the rules, not echo them.
EXPECTED_ALLOWED = {
    (UPLOADED, PROCESSING),
    (PROCESSING, READY),
    (PROCESSING, FAILED),
    (FAILED, PROCESSING),
    (READY, PROCESSING),
}
EXPECTED_REJECTED = sorted(
    set(itertools.product(VideoStatus, VideoStatus)) - EXPECTED_ALLOWED,
    key=lambda pair: (pair[0].value, pair[1].value),
)


@pytest.fixture
def engine():
    engine = create_db_engine("sqlite://")

    # SQLite only enforces foreign keys (and ON DELETE CASCADE) when asked.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def user(db):
    user = User(email="owner@example.com", hashed_password="x")
    db.add(user)
    db.commit()
    return user


def make_video(user: User | None = None, **overrides) -> Video:
    values = {
        "original_filename": "talk.mp4",
        "content_type": "video/mp4",
        "size_bytes": 1024,
        "storage_key": f"users/x/videos/{uuid.uuid4()}/source.mp4",
    }
    values.update(overrides)
    if user is not None:
        values["user"] = user
    return Video(**values)


# --- Model ---------------------------------------------------------------


def test_create_video_with_defaults(db, user):
    video = make_video(user)
    db.add(video)
    db.commit()
    db.refresh(video)

    assert isinstance(video.id, uuid.UUID)
    assert video.user_id == user.id
    assert video.status is UPLOADED
    assert video.error_message is None
    assert video.duration_seconds is None
    assert video.width is None
    assert video.height is None
    assert video.fps is None
    assert video.created_at is not None
    assert video.updated_at is not None


def test_video_user_relationship(db, user):
    first = make_video(user)
    second = make_video(user)
    db.add_all([first, second])
    db.commit()
    db.expire_all()

    loaded = db.get(Video, first.id)
    assert loaded.user.id == user.id
    assert {video.id for video in db.get(User, user.id).videos} == {
        first.id,
        second.id,
    }


def test_media_metadata_can_be_filled_in_later(db, user):
    video = make_video(user)
    db.add(video)
    db.commit()

    video.duration_seconds = 3600.5
    video.width = 1920
    video.height = 1080
    video.fps = 29.97
    db.commit()
    db.expire_all()

    loaded = db.get(Video, video.id)
    assert loaded.duration_seconds == 3600.5
    assert (loaded.width, loaded.height) == (1920, 1080)
    assert loaded.fps == pytest.approx(29.97)


def test_status_is_stored_as_lowercase_value(db, user):
    video = make_video(user, status=READY)
    db.add(video)
    db.commit()

    stored = db.execute(text("SELECT status FROM videos")).scalar_one()
    assert stored == "ready"


def test_unknown_status_is_rejected(db, user):
    db.add(make_video(user, status="archived"))

    with pytest.raises((StatementError, LookupError)):
        db.commit()


def test_video_requires_a_user(db):
    db.add(make_video())

    with pytest.raises(IntegrityError):
        db.commit()


def test_video_user_must_exist(db):
    db.add(make_video(user_id=uuid.uuid4()))

    with pytest.raises(IntegrityError):
        db.commit()


def test_negative_size_is_rejected(db, user):
    db.add(make_video(user, size_bytes=-1))

    with pytest.raises(IntegrityError):
        db.commit()


def test_videos_are_deleted_with_their_user(db, user):
    db.add_all([make_video(user), make_video(user)])
    db.commit()

    db.delete(user)
    db.commit()

    assert db.scalar(select(func.count()).select_from(Video)) == 0


def test_database_cascades_user_delete_to_videos(db, user):
    other = User(email="other@example.com", hashed_password="x")
    db.add_all([make_video(user), make_video(other), other])
    db.commit()

    # A bulk delete bypasses the ORM, so only ON DELETE CASCADE removes rows.
    db.execute(delete(User).where(User.id == user.id))
    db.commit()

    remaining = db.scalars(select(Video)).all()
    assert [video.user_id for video in remaining] == [other.id]


def test_repr_does_not_include_filename_or_storage_key():
    video = make_video(status=UPLOADED, original_filename="secret-plans.mp4")

    assert "secret-plans" not in repr(video)
    assert "users/" not in repr(video)


# --- Status transitions --------------------------------------------------


def test_allowed_transitions_match_the_lifecycle():
    actual = {
        (source, target)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert actual == EXPECTED_ALLOWED
    assert set(ALLOWED_TRANSITIONS) == set(VideoStatus)


@pytest.mark.parametrize(("source", "target"), sorted(EXPECTED_ALLOWED))
def test_allowed_transition_succeeds(source, target):
    video = make_video(status=source)
    error = "boom" if target is FAILED else None

    result = transition_status(video, target, error=error)

    assert result is video
    assert video.status is target


@pytest.mark.parametrize(("source", "target"), EXPECTED_REJECTED)
def test_disallowed_transition_raises_and_leaves_video_unchanged(source, target):
    video = make_video(status=source, error_message="previous")

    with pytest.raises(InvalidStatusTransitionError) as excinfo:
        transition_status(video, target)

    assert video.status is source
    assert video.error_message == "previous"
    assert excinfo.value.current is source
    assert excinfo.value.requested is target


@pytest.mark.parametrize("terminal", [READY, FAILED])
@pytest.mark.parametrize("target", [UPLOADED, READY, FAILED])
def test_terminal_states_only_go_back_to_processing(terminal, target):
    video = make_video(status=terminal)

    with pytest.raises(InvalidStatusTransitionError):
        transition_status(video, target, error="x" if target is FAILED else None)
    assert video.status is terminal


def test_transition_accepts_status_values_as_strings():
    video = make_video(status=UPLOADED)

    transition_status(video, "processing")

    assert video.status is PROCESSING


def test_unknown_target_status_raises_domain_error():
    video = make_video(status=UPLOADED)

    with pytest.raises(InvalidStatusTransitionError):
        transition_status(video, "archived")
    assert video.status is UPLOADED


def test_unknown_current_status_raises_domain_error():
    video = make_video(status=None)

    with pytest.raises(InvalidStatusTransitionError):
        transition_status(video, PROCESSING)


def test_failure_sets_error_message_and_retry_clears_it():
    video = make_video(status=PROCESSING)

    transition_status(video, FAILED, error="  FFmpeg exited with code 1  ")
    assert video.status is FAILED
    assert video.error_message == "FFmpeg exited with code 1"

    transition_status(video, PROCESSING)
    assert video.status is PROCESSING
    assert video.error_message is None


@pytest.mark.parametrize("error", [None, "", "   "])
def test_failure_without_error_uses_generic_message(error):
    video = make_video(status=PROCESSING)

    transition_status(video, FAILED, error=error)

    assert video.error_message == DEFAULT_FAILURE_MESSAGE


def test_ready_clears_stale_error_message():
    video = make_video(status=PROCESSING, error_message="stale")

    transition_status(video, READY)

    assert video.error_message is None


@pytest.mark.parametrize(
    ("source", "target"),
    [(UPLOADED, PROCESSING), (PROCESSING, READY), (FAILED, PROCESSING)],
)
def test_error_is_rejected_for_non_failure_transitions(source, target):
    video = make_video(status=source)

    with pytest.raises(ValueError):
        transition_status(video, target, error="boom")
    assert video.status is source
    assert video.error_message is None


def test_transitions_are_persisted(db, user):
    video = make_video(user)
    db.add(video)
    db.commit()

    transition_status(video, PROCESSING)
    db.commit()
    transition_status(video, FAILED, error="decode error")
    db.commit()
    db.expire_all()

    loaded = db.get(Video, video.id)
    assert loaded.status is FAILED
    assert loaded.error_message == "decode error"


def test_transition_is_logged_without_error_text(caplog):
    video = make_video(status=PROCESSING, id=uuid.uuid4())

    with caplog.at_level(logging.INFO, logger="app.models.video"):
        transition_status(video, FAILED, error="private details")

    records = [r for r in caplog.records if r.getMessage() == "video_status_changed"]
    assert len(records) == 1
    record = records[0]
    assert record.video_id == str(video.id)
    assert record.from_status == "processing"
    assert record.to_status == "failed"
    assert "private details" not in caplog.text


# --- Schemas -------------------------------------------------------------


def test_video_read_from_model(db, user):
    video = make_video(user)
    db.add(video)
    db.commit()
    db.refresh(video)

    data = VideoRead.model_validate(video).model_dump(mode="json")

    assert data["id"] == str(video.id)
    assert data["user_id"] == str(user.id)
    assert data["status"] == "uploaded"
    assert data["original_filename"] == "talk.mp4"
    assert data["size_bytes"] == 1024
    assert data["error_message"] is None
    assert "storage_key" not in data


def test_video_list_wraps_items(db, user):
    video = make_video(user)
    db.add(video)
    db.commit()
    db.refresh(video)

    listing = VideoList(items=[VideoRead.model_validate(video)], total=1)

    data = listing.model_dump(mode="json")
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [str(video.id)]


def test_video_list_rejects_negative_total():
    with pytest.raises(ValidationError):
        VideoList(items=[], total=-1)
