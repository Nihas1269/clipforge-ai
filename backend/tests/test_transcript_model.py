import logging
import uuid

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.database import Base, create_db_engine
from app.models.transcript import Transcript, TranscriptSegment
from app.models.user import User
from app.models.video import Video
from app.repositories.transcripts import get_transcript_for_video, replace_transcript
from app.services.video_deletion import delete_video
from app.storage import LocalStorage, video_source_key
from app.transcription import (
    FakeTranscriptionProvider,
    InvalidLanguageError,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
)

# Code-switched and non-Latin text in the launch languages; storage must keep
# the original script byte for byte.
UNICODE_SEGMENTS = [
    ("te", "నేను ఈరోజు office కి వెళ్తున్నాను"),
    ("hi", "मैं kal meeting में आऊँगा"),
    ("ta", "வணக்கம் everyone, எப்படி இருக்கீங்க?"),
    ("kn", "ನಮಸ್ಕಾರ friends"),
    ("ml", "നമസ്കാരം world"),
    ("en", "Emoji and accents: café 🎬"),
]


def count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


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


@pytest.fixture
def video(db, user):
    video_id = uuid.uuid4()
    video = Video(
        id=video_id,
        user=user,
        original_filename="talk.mp4",
        content_type="video/mp4",
        size_bytes=1024,
        storage_key=video_source_key(str(user.id), str(video_id), ".mp4"),
        duration_seconds=12.5,
    )
    db.add(video)
    db.commit()
    return video


def make_transcript(video: Video, segments=(), **overrides) -> Transcript:
    values = {
        "video_id": video.id,
        "language": "en",
        "provider": "fake",
        "full_text": "",
        "segments": list(segments),
    }
    values.update(overrides)
    return Transcript(**values)


def make_segment(index: int, start: float, end: float, **overrides) -> TranscriptSegment:
    values = {
        "index": index,
        "start_seconds": start,
        "end_seconds": end,
        "text": f"segment {index}",
    }
    values.update(overrides)
    return TranscriptSegment(**values)


# --- Persistence ---------------------------------------------------------


def test_transcript_is_persisted_with_segments_ordered_by_index(db, video):
    words = [
        {"word": "segment", "start": 0.0, "end": 0.5},
        {"word": "0", "start": 0.5, "end": 1.0},
    ]
    transcript = make_transcript(
        video,
        # Added out of order on purpose.
        [
            make_segment(2, 10.0, 12.5),
            make_segment(0, 0.0, 5.0, words=words),
            make_segment(1, 5.0, 10.0),
        ],
        full_text="segment 0 segment 1 segment 2",
        model="fake-v1",
    )
    db.add(transcript)
    db.commit()
    db.expire_all()

    loaded = db.get(Transcript, transcript.id)
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.video_id == video.id
    assert loaded.video.id == video.id
    assert db.get(Video, video.id).transcript.id == transcript.id
    assert loaded.language == "en"
    assert loaded.requested_language is None
    assert loaded.provider == "fake"
    assert loaded.model == "fake-v1"
    assert loaded.full_text == "segment 0 segment 1 segment 2"
    assert loaded.created_at is not None
    assert [s.index for s in loaded.segments] == [0, 1, 2]
    assert [(s.start_seconds, s.end_seconds) for s in loaded.segments] == [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 12.5),
    ]
    assert loaded.segments[0].words == words
    assert loaded.segments[1].words is None


def test_missing_word_timings_are_stored_as_sql_null(db, video):
    db.add(make_transcript(video, [make_segment(0, 0.0, 1.0, words=None)]))
    db.commit()

    assert db.execute(text("SELECT words FROM transcript_segments")).scalar_one() is None


def test_empty_transcript_without_segments(db, video):
    db.add(make_transcript(video, language=None, full_text=""))
    db.commit()
    db.expire_all()

    loaded = db.scalars(select(Transcript)).one()
    assert loaded.segments == []
    assert loaded.full_text == ""
    assert loaded.language is None


@pytest.mark.parametrize("language", [None, "und", "te", "hi-IN"])
def test_unknown_and_regional_languages_are_stored_as_given(db, video, language):
    db.add(make_transcript(video, language=language, requested_language=language))
    db.commit()
    db.expire_all()

    loaded = db.scalars(select(Transcript)).one()
    assert loaded.language == language
    assert loaded.requested_language == language


def test_unicode_and_code_switched_text_round_trips(db, video):
    segments = []
    for index, (_language, segment_text) in enumerate(UNICODE_SEGMENTS):
        first_word = segment_text.split()[0]
        segments.append(
            make_segment(
                index,
                index * 2.0,
                index * 2.0 + 1.5,
                text=segment_text,
                words=[{"word": first_word, "start": index * 2.0, "end": index * 2.0 + 0.4}],
            )
        )
    full_text = " ".join(segment_text for _language, segment_text in UNICODE_SEGMENTS)
    db.add(make_transcript(video, segments, language="te", full_text=full_text))
    db.commit()
    db.expire_all()

    loaded = db.scalars(select(Transcript)).one()
    assert loaded.full_text == full_text
    assert [s.text for s in loaded.segments] == [t for _l, t in UNICODE_SEGMENTS]
    assert [s.words[0]["word"] for s in loaded.segments] == [
        t.split()[0] for _l, t in UNICODE_SEGMENTS
    ]


def test_repr_does_not_include_text(video):
    transcript = make_transcript(video, full_text="PRIVATE-WORDS")
    segment = make_segment(0, 0.0, 1.0, text="PRIVATE-WORDS")

    assert "PRIVATE-WORDS" not in repr(transcript)
    assert "PRIVATE-WORDS" not in repr(segment)


# --- Constraints ---------------------------------------------------------


def test_a_video_has_at_most_one_transcript(db, video):
    db.add(make_transcript(video))
    db.commit()

    db.add(make_transcript(video))
    with pytest.raises(IntegrityError):
        db.commit()


def test_transcript_requires_an_existing_video(db):
    db.add(Transcript(video_id=uuid.uuid4(), provider="fake", full_text=""))

    with pytest.raises(IntegrityError):
        db.commit()


def test_segment_requires_an_existing_transcript(db, video):
    db.add(make_segment(0, 0.0, 1.0, transcript_id=uuid.uuid4()))

    with pytest.raises(IntegrityError):
        db.commit()


def test_segment_indexes_are_unique_per_transcript(db, video, user):
    db.add(make_transcript(video, [make_segment(0, 0.0, 1.0), make_segment(0, 1.0, 2.0)]))

    with pytest.raises(IntegrityError):
        db.commit()


def test_the_same_index_is_allowed_in_different_transcripts(db, video, user):
    other_id = uuid.uuid4()
    other = Video(
        id=other_id,
        user=user,
        original_filename="b.mp4",
        content_type="video/mp4",
        size_bytes=1,
        storage_key=video_source_key(str(user.id), str(other_id), ".mp4"),
    )
    db.add_all(
        [
            other,
            make_transcript(video, [make_segment(0, 0.0, 1.0)]),
            make_transcript(other, [make_segment(0, 0.0, 1.0)]),
        ]
    )
    db.commit()

    assert count(db, TranscriptSegment) == 2


@pytest.mark.parametrize(
    ("index", "start", "end"),
    [
        (-1, 0.0, 1.0),  # negative index
        (0, -0.5, 1.0),  # negative start
        (0, 1.0, 1.0),  # zero length
        (0, 2.0, 1.0),  # ends before it starts
    ],
)
def test_invalid_segments_are_rejected(db, video, index, start, end):
    db.add(make_transcript(video, [make_segment(index, start, end)]))

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("column", ["provider", "full_text"])
def test_required_transcript_columns(db, video, column):
    db.add(make_transcript(video, **{column: None}))

    with pytest.raises(IntegrityError):
        db.commit()


# --- Cascades ------------------------------------------------------------


def add_transcript_with_segments(db, video):
    db.add(make_transcript(video, [make_segment(0, 0.0, 5.0), make_segment(1, 5.0, 9.0)]))
    db.commit()


def test_database_cascades_video_delete_to_transcript(db, video):
    add_transcript_with_segments(db, video)

    # A bulk delete bypasses the ORM, so only ON DELETE CASCADE removes rows.
    db.execute(delete(Video).where(Video.id == video.id))
    db.commit()

    assert count(db, Transcript) == 0
    assert count(db, TranscriptSegment) == 0


def test_orm_video_delete_removes_transcript(db, video):
    add_transcript_with_segments(db, video)

    db.delete(video)
    db.commit()

    assert count(db, Transcript) == 0
    assert count(db, TranscriptSegment) == 0


def test_user_delete_cascades_to_transcripts(db, user, video):
    add_transcript_with_segments(db, video)

    db.execute(delete(User).where(User.id == user.id))
    db.commit()

    assert count(db, Transcript) == 0
    assert count(db, TranscriptSegment) == 0


def test_delete_video_service_removes_transcript(db, user, video, tmp_path):
    add_transcript_with_segments(db, video)

    delete_video(db, LocalStorage(tmp_path), video.id, user.id)

    assert count(db, Video) == 0
    assert count(db, Transcript) == 0
    assert count(db, TranscriptSegment) == 0


def test_delete_video_service_removes_transcript_without_enforced_foreign_keys(
    tmp_path,
):
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(email="owner@example.com", hashed_password="x")
            video_id = uuid.uuid4()
            video = Video(
                id=video_id,
                user=user,
                original_filename="a.mp4",
                content_type="video/mp4",
                size_bytes=1,
                storage_key=video_source_key(str(uuid.uuid4()), str(video_id), ".mp4"),
            )
            db.add_all([user, video])
            db.commit()
            add_transcript_with_segments(db, video)

            delete_video(db, LocalStorage(tmp_path), video.id, user.id)

            assert count(db, Transcript) == 0
            assert count(db, TranscriptSegment) == 0
    finally:
        engine.dispose()


def test_deleting_a_transcript_removes_its_segments(db, video):
    add_transcript_with_segments(db, video)

    db.execute(delete(Transcript))
    db.commit()

    assert count(db, TranscriptSegment) == 0
    assert count(db, Video) == 1


# --- Repository ----------------------------------------------------------


def make_result(**overrides) -> TranscriptionResult:
    values = {
        "language": "hi",
        "segments": (
            TranscriptionSegment(
                0.0,
                1.25,
                "मैं kal",
                words=(WordTiming("मैं", 0.0, 0.5), WordTiming("kal", 0.5, 1.25)),
            ),
            TranscriptionSegment(1.25, 3.333, "आऊँगा"),
        ),
        "provider": "fake",
        "model": "fake-v1",
    }
    values.update(overrides)
    return TranscriptionResult(**values)


def test_replace_transcript_stores_the_result_faithfully(db, video):
    replace_transcript(db, video.id, make_result(), requested_language="auto")
    db.commit()
    db.expire_all()

    loaded = get_transcript_for_video(db, video.id)
    assert loaded.language == "hi"
    assert loaded.requested_language is None
    assert loaded.provider == "fake"
    assert loaded.model == "fake-v1"
    assert loaded.full_text == "मैं kal आऊँगा"
    assert [(s.index, s.start_seconds, s.end_seconds, s.text) for s in loaded.segments] == [
        (0, 0.0, 1.25, "मैं kal"),
        (1, 1.25, 3.333, "आऊँगा"),
    ]
    assert loaded.segments[0].words == [
        {"word": "मैं", "start": 0.0, "end": 0.5},
        {"word": "kal", "start": 0.5, "end": 1.25},
    ]
    assert loaded.segments[1].words is None


def test_replace_transcript_keeps_requested_and_reported_language_apart(db, video):
    # The user asked for Telugu; the provider reported Hindi. Neither is
    # relabeled to match the other.
    replace_transcript(db, video.id, make_result(language="hi"), requested_language="te")
    db.commit()
    db.expire_all()

    loaded = get_transcript_for_video(db, video.id)
    assert loaded.language == "hi"
    assert loaded.requested_language == "te"


def test_replace_transcript_rejects_invalid_requested_language(db, video):
    with pytest.raises(InvalidLanguageError):
        replace_transcript(db, video.id, make_result(), requested_language="telugu")

    assert count(db, Transcript) == 0


def test_replace_transcript_replaces_instead_of_duplicating(db, video):
    replace_transcript(db, video.id, make_result())
    db.commit()
    replacement = make_result(
        language="en", segments=(TranscriptionSegment(0.0, 2.0, "Hello"),)
    )

    replace_transcript(db, video.id, replacement)
    db.commit()
    db.expire_all()

    assert count(db, Transcript) == 1
    assert count(db, TranscriptSegment) == 1
    loaded = get_transcript_for_video(db, video.id)
    assert loaded.language == "en"
    assert [s.text for s in loaded.segments] == ["Hello"]


def test_replace_transcript_with_zero_segments(db, video):
    replace_transcript(db, video.id, make_result(language=None, segments=()))
    db.commit()
    db.expire_all()

    loaded = get_transcript_for_video(db, video.id)
    assert loaded.segments == []
    assert loaded.full_text == ""
    assert loaded.language is None


def test_fake_provider_output_can_be_stored(db, video, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"\x00")
    provider = FakeTranscriptionProvider(duration_probe=lambda _path: video.duration_seconds)

    result = provider.transcribe(audio)
    replace_transcript(db, video.id, result)
    db.commit()
    db.expire_all()

    loaded = get_transcript_for_video(db, video.id)
    assert [(s.start_seconds, s.end_seconds) for s in loaded.segments] == [
        (s.start, s.end) for s in result.segments
    ]
    assert loaded.segments[-1].end_seconds <= video.duration_seconds
    assert [s.words for s in loaded.segments] == [
        [w.to_dict() for w in s.words] for s in result.segments
    ]


def test_replace_transcript_logs_without_text(db, video, caplog):
    with caplog.at_level(logging.INFO, logger="app.repositories.transcripts"):
        replace_transcript(db, video.id, make_result())

    (record,) = [r for r in caplog.records if r.getMessage() == "transcript_saved"]
    assert record.video_id == str(video.id)
    assert record.segment_count == 2
    assert "kal" not in caplog.text
