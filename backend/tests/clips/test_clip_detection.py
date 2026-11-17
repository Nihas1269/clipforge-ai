"""Tests for detector selection and the clip detection endpoint."""

import logging
import uuid

import pytest

from app.clips import (
    HeuristicClipDetector,
    LLMClipDetector,
    get_clip_detector,
)
from app.core.config import Settings, get_settings
from app.models.clip import (
    ALLOWED_CLIP_STATUS_TRANSITIONS,
    ClipSource,
    ClipStatus,
    is_allowed_clip_status_change,
)
from app.models.processing_job import JobStatus, JobType
from app.models.video import VideoStatus
from app.worker import clip_detection as clip_worker
from app.worker.celery_app import TASK_MODULES
from app.worker.clip_detection import (
    CLIP_DETECTION_MAX_ATTEMPTS,
    DETECT_CLIPS_TASK_NAME,
    detect_clips,
)
from app.worker.jobs import JobTask
from tests.clips.helpers import (
    DURATION,
    FIRST_RUN,
    PRIVATE_WORD,
    clip_url,
    clips_url,
    detect_url,
    detection_jobs,
    make_video,
    manual_clip,
    ranges,
    ranges_of,
    stored_clips,
)

# Detector selection


def test_default_detector_is_heuristic():
    detector = get_clip_detector(Settings())
    assert isinstance(detector, HeuristicClipDetector)
    assert detector.version == "heuristic-1"


def test_unknown_detector_falls_back_to_heuristic(caplog):
    settings = Settings(CLIP_DETECTOR=" Magic ")
    assert settings.CLIP_DETECTOR == "magic"

    detector = get_clip_detector(settings)

    assert isinstance(detector, HeuristicClipDetector)
    [warning] = [r for r in caplog.records if r.getMessage() == "clip_detector_unknown"]
    assert warning.levelno == logging.WARNING
    assert warning.fallback == "heuristic"


def test_llm_detector_is_selected_without_llm_settings():
    detector = get_clip_detector(Settings(CLIP_DETECTOR="llm", LLM_PROVIDER=""))

    assert isinstance(detector, LLMClipDetector)
    assert detector.client is None
    assert isinstance(detector.fallback, HeuristicClipDetector)


def test_status_transitions_are_defined():
    assert is_allowed_clip_status_change(ClipStatus.SUGGESTED, ClipStatus.ACCEPTED)
    assert is_allowed_clip_status_change(ClipStatus.SUGGESTED, ClipStatus.REJECTED)
    assert is_allowed_clip_status_change(ClipStatus.ACCEPTED, ClipStatus.REJECTED)
    assert is_allowed_clip_status_change(ClipStatus.REJECTED, ClipStatus.ACCEPTED)
    for status in ClipStatus:
        assert is_allowed_clip_status_change(status, status)
        if status is not ClipStatus.SUGGESTED:
            assert not is_allowed_clip_status_change(status, ClipStatus.SUGGESTED)
    assert set(ALLOWED_CLIP_STATUS_TRANSITIONS) == set(ClipStatus)


# Detection


def test_task_is_registered_with_the_worker():
    assert "app.worker.clip_detection" in TASK_MODULES
    assert detect_clips.name == DETECT_CLIPS_TASK_NAME
    assert detect_clips.job_type == JobType.CLIP_DETECTION
    assert detect_clips.job_max_attempts == CLIP_DETECTION_MAX_ATTEMPTS
    assert detect_clips.soft_time_limit < detect_clips.time_limit


def test_detection_creates_suggestions(
    client, db, alice, transcribed_video_id, eager_jobs
):
    response = client.post(detect_url(transcribed_video_id), headers=alice["headers"])

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status"}
    assert body["status"] == "succeeded"
    [job] = detection_jobs(db)
    assert str(job.id) == body["job_id"]
    assert job.status == JobStatus.SUCCEEDED
    assert job.video_id == transcribed_video_id
    assert job.user_id == alice["id"]
    assert job.payload == {"video_id": str(transcribed_video_id)}
    assert job.idempotency_key == str(transcribed_video_id)
    assert job.active_key is None

    listing = client.get(clips_url(transcribed_video_id), headers=alice["headers"])
    assert listing.status_code == 200
    data = listing.json()
    assert data["total"] == len(FIRST_RUN)
    assert ranges(data["items"]) == FIRST_RUN
    for item in data["items"]:
        assert item["video_id"] == str(transcribed_video_id)
        assert item["status"] == "suggested"
        assert item["source"] == "heuristic"
        assert item["detector_version"] == "heuristic-1"
        assert item["detector_model"] is None
        assert item["prompt_version"] is None
        assert 0 <= item["score"] <= 1
        assert item["title"]
        assert item["reason"]
        assert 0 <= item["start_seconds"] < item["end_seconds"] <= DURATION


def test_detection_queues_one_job_per_video(
    client, db, alice, transcribed_video_id, published_jobs
):
    first = client.post(detect_url(transcribed_video_id), headers=alice["headers"])
    second = client.post(detect_url(transcribed_video_id), headers=alice["headers"])

    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "clip_detection_in_progress"
    [job] = detection_jobs(db)
    assert published_jobs == [
        {
            "task": DETECT_CLIPS_TASK_NAME,
            "args": [str(job.id)],
            "task_id": job.celery_task_id,
        }
    ]


def test_detection_without_transcript_is_409(client, db, alice, video_id):
    response = client.post(detect_url(video_id), headers=alice["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transcript_required"
    assert detection_jobs(db) == []


@pytest.mark.parametrize(
    "video_status", [VideoStatus.UPLOADED, VideoStatus.PROCESSING, VideoStatus.FAILED]
)
def test_detection_of_unready_video_is_409(
    client, db, alice, session_factory, storage_dir, video_status
):
    video_id = make_video(
        session_factory, storage_dir, alice["id"], status=video_status
    )

    response = client.post(detect_url(video_id), headers=alice["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "video_not_ready"
    assert detection_jobs(db) == []


def test_detection_queue_failure_is_503(
    client, db, alice, transcribed_video_id, monkeypatch
):
    def broken_apply_async(self, *args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(JobTask, "apply_async", broken_apply_async)

    response = client.post(detect_url(transcribed_video_id), headers=alice["headers"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_unavailable"
    [job] = detection_jobs(db)
    assert job.status == JobStatus.FAILED
    assert job.active_key is None


def test_redetection_preserves_accepted_and_manual_clips(
    client, db, alice, transcribed_video_id, eager_jobs
):
    headers = alice["headers"]
    assert (
        client.post(detect_url(transcribed_video_id), headers=headers).json()["status"]
        == "succeeded"
    )
    first = {
        (clip.start_seconds, clip.end_seconds): clip.id
        for clip in stored_clips(db, transcribed_video_id)
    }
    accepted_id = first[(0.0, 20.0)]
    rejected_id = first[(20.0, 40.0)]
    assert (
        client.patch(
            clip_url(accepted_id), json={"status": "accepted"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.patch(
            clip_url(rejected_id), json={"status": "rejected"}, headers=headers
        ).status_code
        == 200
    )
    manual = manual_clip(client, transcribed_video_id, headers, start=45.0, end=70.0)

    response = client.post(detect_url(transcribed_video_id), headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    clips = {
        (clip.start_seconds, clip.end_seconds): clip
        for clip in stored_clips(db, transcribed_video_id)
    }
    # Kept untouched: the accepted, rejected and manual clips.
    assert clips[(0.0, 20.0)].id == accepted_id
    assert clips[(0.0, 20.0)].status is ClipStatus.ACCEPTED
    assert clips[(20.0, 40.0)].id == rejected_id
    assert clips[(20.0, 40.0)].status is ClipStatus.REJECTED
    assert clips[(45.0, 70.0)].id == uuid.UUID(manual["id"])
    assert clips[(45.0, 70.0)].source is ClipSource.MANUAL
    # Old suggestions were replaced; new ones avoid the accepted and manual
    # clips and do not repeat the rejected one.
    assert set(clips) == {
        (0.0, 20.0),
        (20.0, 40.0),
        (45.0, 70.0),
        (80.0, 100.0),
        (100.0, 120.0),
    }
    for key in ((80.0, 100.0), (100.0, 120.0)):
        assert clips[key].status is ClipStatus.SUGGESTED
        assert clips[key].id not in set(first.values())

    listing = client.get(clips_url(transcribed_video_id), headers=headers).json()
    # Manual clips score 1; the equal detector scores are ordered by start.
    assert ranges(listing["items"]) == [
        (45.0, 70.0),
        (0.0, 20.0),
        (20.0, 40.0),
        (80.0, 100.0),
        (100.0, 120.0),
    ]
    accepted = client.get(
        clips_url(transcribed_video_id), params={"status": "accepted"}, headers=headers
    ).json()
    assert ranges(accepted["items"]) == [(45.0, 70.0), (0.0, 20.0)]
    assert accepted["total"] == 2


def test_failed_save_keeps_previous_suggestions(
    client, db, alice, transcribed_video_id, eager_jobs, monkeypatch
):
    headers = alice["headers"]
    client.post(detect_url(transcribed_video_id), headers=headers)
    before = {clip.id for clip in stored_clips(db, transcribed_video_id)}
    original = clip_worker.replace_suggestions

    def replace_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("database went away with zebraprivate")

    monkeypatch.setattr(clip_worker, "replace_suggestions", replace_then_fail)

    response = client.post(detect_url(transcribed_video_id), headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert {clip.id for clip in stored_clips(db, transcribed_video_id)} == before
    job = detection_jobs(db)[-1]
    assert job.error_message == "The job failed unexpectedly (RuntimeError)."
    assert PRIVATE_WORD not in job.error_message


def test_unknown_detector_runs_the_heuristic_with_a_warning(
    client, db, alice, transcribed_video_id, eager_jobs, monkeypatch, caplog
):
    monkeypatch.setenv("CLIP_DETECTOR", "secret-llm")
    get_settings.cache_clear()

    response = client.post(detect_url(transcribed_video_id), headers=alice["headers"])

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    assert any(r.getMessage() == "clip_detector_unknown" for r in caplog.records)
    clips = stored_clips(db, transcribed_video_id)
    assert ranges_of(clips) == FIRST_RUN
    assert {c.source for c in clips} == {ClipSource.HEURISTIC}
    assert {c.detector_version for c in clips} == {"heuristic-1"}


def test_detection_logs_no_transcript_text(
    client, alice, transcribed_video_id, eager_jobs, caplog
):
    caplog.set_level(logging.DEBUG, logger="app")

    client.post(detect_url(transcribed_video_id), headers=alice["headers"])

    records = [r for r in caplog.records if r.name.startswith("app.")]
    assert any(r.getMessage() == "clip_detection_saved" for r in records)
    [replaced] = [r for r in records if r.getMessage() == "clip_suggestions_replaced"]
    assert replaced.created_count == len(FIRST_RUN)
    for record in records:
        assert PRIVATE_WORD not in str(record.__dict__)
