"""Shared constants and helpers for the clip detection and review tests."""

import io
import uuid

from sqlalchemy import select

from app.models.clip import Clip
from app.models.processing_job import JobType, ProcessingJob
from app.models.transcript import Transcript, TranscriptSegment
from app.models.video import Video, VideoStatus
from app.storage import LocalStorage, video_source_key

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
FRONTEND_ORIGIN = "http://localhost:3000"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"

DURATION = 120.0
SEGMENT_SECONDS = 10.0
# The same wording in every segment gives every window the same score, so
# the heuristic picks the earliest non-overlapping 20-second windows.
SEGMENT_TEXT = "We talk about video editing and share a quick idea here zebraprivate."
PRIVATE_WORD = "zebraprivate"
FIRST_RUN = [
    (0.0, 20.0),
    (20.0, 40.0),
    (40.0, 60.0),
    (60.0, 80.0),
    (80.0, 100.0),
    (100.0, 120.0),
]


def detect_url(video_id) -> str:
    return f"/api/v1/videos/{video_id}/clips/detect"


def clips_url(video_id) -> str:
    return f"/api/v1/videos/{video_id}/clips"


def clip_url(clip_id) -> str:
    return f"/api/v1/clips/{clip_id}"


def register(client, email) -> uuid.UUID:
    response = client.post(REGISTER_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def auth_headers(client, email) -> dict[str, str]:
    response = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_video(
    session_factory,
    storage_dir,
    user_id,
    *,
    status=VideoStatus.READY,
    duration=DURATION,
) -> uuid.UUID:
    video_id = uuid.uuid4()
    key = video_source_key(str(user_id), str(video_id), ".mp4")
    LocalStorage(storage_dir).save(key, io.BytesIO(b"not-really-a-video"))
    with session_factory() as session:
        session.add(
            Video(
                id=video_id,
                user_id=user_id,
                original_filename="talk.mp4",
                content_type="video/mp4",
                size_bytes=18,
                storage_key=key,
                status=status,
                duration_seconds=duration,
                width=1280,
                height=720,
                fps=30.0,
            )
        )
        session.commit()
    return video_id


def add_transcript(session_factory, video_id, *, seconds=DURATION) -> None:
    count = int(seconds // SEGMENT_SECONDS)
    with session_factory() as session:
        session.add(
            Transcript(
                video_id=video_id,
                language="en",
                provider="fake",
                model="fake-v1",
                full_text=" ".join([SEGMENT_TEXT] * count),
                segments=[
                    TranscriptSegment(
                        index=index,
                        start_seconds=index * SEGMENT_SECONDS,
                        end_seconds=(index + 1) * SEGMENT_SECONDS,
                        text=SEGMENT_TEXT,
                    )
                    for index in range(count)
                ],
            )
        )
        session.commit()


def stored_clips(db, video_id) -> list[Clip]:
    db.expire_all()
    return list(
        db.scalars(
            select(Clip)
            .where(Clip.video_id == video_id)
            .order_by(Clip.start_seconds, Clip.end_seconds)
        )
    )


def detection_jobs(db) -> list[ProcessingJob]:
    db.expire_all()
    return list(
        db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.job_type == JobType.CLIP_DETECTION)
            .order_by(ProcessingJob.created_at)
        )
    )


def ranges(items) -> list[tuple[float, float]]:
    return [(item["start_seconds"], item["end_seconds"]) for item in items]


def ranges_of(clips) -> list[tuple[float, float]]:
    return [(clip.start_seconds, clip.end_seconds) for clip in clips]


def create_clip(client, video_id, headers, **body):
    return client.post(clips_url(video_id), json=body, headers=headers)


def manual_clip(client, video_id, headers, start=10.0, end=40.0) -> dict:
    response = create_clip(
        client, video_id, headers, start_seconds=start, end_seconds=end
    )
    assert response.status_code == 201
    return response.json()
