import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.database import Base, create_db_engine
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.user import User
from app.models.video import Video
from app.services.video_deletion import delete_video

INSERT_CLIP = text(
    "INSERT INTO clips (id, video_id, start_seconds, end_seconds, title, reason, "
    "score, status, source) VALUES (:id, :video_id, :start, :end, 'T', 'R', "
    ":score, :status, :source)"
)


def _make_engine(foreign_keys: bool):
    engine = create_db_engine("sqlite://")

    if foreign_keys:
        # SQLite only enforces foreign keys (and ON DELETE CASCADE) when asked.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def engine():
    engine = _make_engine(foreign_keys=True)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


def _video(db) -> Video:
    user = User(email=f"{uuid.uuid4().hex}@example.com", hashed_password="x")
    db.add(user)
    db.flush()
    video = Video(
        user_id=user.id,
        original_filename="talk.mp4",
        content_type="video/mp4",
        size_bytes=1,
        storage_key=f"users/{user.id}/videos/v/source.mp4",
    )
    db.add(video)
    db.commit()
    return video


def _clip(video, **overrides) -> Clip:
    values = {
        "video_id": video.id,
        "start_seconds": 10.0,
        "end_seconds": 40.0,
        "title": "A moment",
        "reason": "Dense speech.",
        "score": 0.75,
        "source": ClipSource.HEURISTIC,
    }
    values.update(overrides)
    return Clip(**values)


def test_clip_defaults(db):
    video = _video(db)
    clip = _clip(video)
    db.add(clip)
    db.commit()
    db.refresh(clip)

    assert clip.status is ClipStatus.SUGGESTED
    assert clip.source is ClipSource.HEURISTIC
    assert clip.created_at is not None
    assert clip.updated_at is not None
    assert "A moment" not in repr(clip)


def test_video_clips_are_ordered_by_start(db):
    video = _video(db)
    db.add_all([_clip(video, start_seconds=50, end_seconds=70), _clip(video)])
    db.commit()
    db.expire_all()

    assert [clip.start_seconds for clip in video.clips] == [10.0, 50.0]


@pytest.mark.parametrize(
    "values",
    [
        {"start": -1.0, "end": 20.0, "score": 0.5},
        {"start": 20.0, "end": 20.0, "score": 0.5},
        {"start": 30.0, "end": 20.0, "score": 0.5},
        {"start": 0.0, "end": 20.0, "score": 1.01},
        {"start": 0.0, "end": 20.0, "score": -0.01},
        {"status": "maybe"},
        {"source": "magic"},
    ],
)
def test_database_rejects_invalid_clips(db, values):
    video = _video(db)
    params = {
        "id": uuid.uuid4().hex,
        "video_id": video.id.hex,
        "start": 0.0,
        "end": 20.0,
        "score": 0.5,
        "status": "suggested",
        "source": "heuristic",
        **values,
    }

    with pytest.raises(IntegrityError):
        db.execute(INSERT_CLIP, params)
        db.commit()
    db.rollback()


def test_database_accepts_score_bounds(db):
    video = _video(db)
    for score in (0.0, 1.0):
        db.execute(
            INSERT_CLIP,
            {
                "id": uuid.uuid4().hex,
                "video_id": video.id.hex,
                "start": 0.0,
                "end": 20.0,
                "score": score,
                "status": "accepted",
                "source": "manual",
            },
        )
    db.commit()

    assert len(db.scalars(select(Clip)).all()) == 2


def test_clip_requires_existing_video(db):
    db.add(_clip(SimpleNamespace(id=uuid.uuid4())))

    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_video_cascades_to_clips(db):
    video = _video(db)
    db.add(_clip(video))
    db.commit()

    db.execute(delete(Video).where(Video.id == video.id))
    db.commit()

    assert db.scalars(select(Clip)).all() == []


def test_delete_video_removes_clips_without_foreign_keys():
    engine = _make_engine(foreign_keys=False)
    try:
        with Session(engine) as db:
            video = _video(db)
            other = _video(db)
            db.add_all([_clip(video), _clip(other)])
            db.commit()

            delete_video(db, MagicMock(), video.id, video.user_id)

            remaining = db.scalars(select(Clip.video_id)).all()
            assert remaining == [other.id]
    finally:
        engine.dispose()
