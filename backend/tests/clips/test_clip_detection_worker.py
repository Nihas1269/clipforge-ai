"""Tests for the clip detection worker run directly against the database."""

import pytest

from app.clips import HeuristicClipDetector, LLMClipDetector
from app.clips.llm_prompt import PROMPT_VERSION
from app.models.clip import ClipSource
from app.worker.clip_detection import (
    DETECTOR_UNAVAILABLE_MESSAGE,
    INVALID_RESULT_MESSAGE,
    NO_TRANSCRIPT_MESSAGE,
    run_clip_detection,
)
from app.worker.jobs import JobError
from tests.clips.helpers import FIRST_RUN, stored_clips


class ScriptedDetector:
    name = "heuristic"
    version = "scripted-1"

    def __init__(self, result):
        self.result = result

    def detect(self, segments, video_duration, options):
        return self.result


def test_worker_rejects_invalid_detector_output(db, transcribed_video_id):
    with pytest.raises(JobError) as raised:
        run_clip_detection(
            db, transcribed_video_id, detector=ScriptedDetector([{"start": 0}])
        )

    assert raised.value.public_message == INVALID_RESULT_MESSAGE
    assert stored_clips(db, transcribed_video_id) == []


def test_worker_rejects_detector_with_unknown_source(db, transcribed_video_id):
    detector = ScriptedDetector([])
    detector.name = "manual"

    with pytest.raises(JobError) as raised:
        run_clip_detection(db, transcribed_video_id, detector=detector)

    assert raised.value.public_message == DETECTOR_UNAVAILABLE_MESSAGE


def test_worker_requires_a_transcript(db, video_id):
    with pytest.raises(JobError) as raised:
        run_clip_detection(db, video_id)

    assert raised.value.public_message == NO_TRANSCRIPT_MESSAGE


def test_cancelled_detection_saves_nothing(db, transcribed_video_id):
    result = run_clip_detection(db, transcribed_video_id, is_cancelled=lambda: True)

    assert result is None
    assert stored_clips(db, transcribed_video_id) == []


def test_worker_records_detector_provenance(db, transcribed_video_id):
    clips = run_clip_detection(
        db, transcribed_video_id, detector=HeuristicClipDetector(keywords=[])
    )

    assert clips is not None
    assert [(c.start_seconds, c.end_seconds) for c in clips] == FIRST_RUN
    assert {c.detector_version for c in clips} == {"heuristic-1"}
    assert {c.source for c in clips} == {ClipSource.HEURISTIC}


class FakeLLMClient:
    provider = "openai_compatible"
    model = "fake-model-1"
    last_usage = None

    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user):
        return self.answer


def test_worker_records_llm_provenance(db, transcribed_video_id):
    answer = (
        '[{"start_seconds": 30, "end_seconds": 60, "title": "Editing idea",'
        ' "reason": "A quick tip.", "score": 0.8}]'
    )
    detector = LLMClipDetector(FakeLLMClient(answer))

    clips = run_clip_detection(db, transcribed_video_id, detector=detector)

    assert clips is not None
    [clip] = stored_clips(db, transcribed_video_id)
    assert (clip.start_seconds, clip.end_seconds) == (30.0, 60.0)
    assert clip.source is ClipSource.LLM
    assert clip.detector_version == "llm"
    assert clip.detector_model == "fake-model-1"
    assert clip.prompt_version == PROMPT_VERSION


def test_worker_records_fallback_provenance_when_llm_fails(db, transcribed_video_id):
    detector = LLMClipDetector(FakeLLMClient("not json"))

    clips = run_clip_detection(db, transcribed_video_id, detector=detector)

    assert clips
    assert detector.last_run is not None and detector.last_run.fallback_used
    for clip in stored_clips(db, transcribed_video_id):
        assert clip.source is ClipSource.HEURISTIC
        assert clip.detector_version == "heuristic-1"
        assert clip.detector_model is None
        assert clip.prompt_version is None
