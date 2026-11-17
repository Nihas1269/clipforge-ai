import httpx
import pytest

from app.media import ffmpeg as ffmpeg_module
from app.media.ffmpeg import MediaError
from app.transcription import (
    InvalidTranscriptionResultError,
    TranscriptionError,
    TranscriptionProviderError,
)
from app.transcription.openai_compatible import (
    BYTES_PER_MB,
    chunk_seconds_for,
    ffmpeg_write_chunk,
    plan_chunks,
)
from tests.transcription_support import (
    fixed_duration,
    json_handler,
    make_provider,
    verbose_json,
)

# --- OpenAI-compatible: chunking ------------------------------------------


def test_plan_chunks():
    assert plan_chunks(0, 10) == []
    assert plan_chunks(5, 10) == [(0, 5)]
    assert plan_chunks(10, 10) == [(0, 10)]
    assert plan_chunks(25, 10) == [(0, 10), (10, 10), (20, 5)]
    # A last piece shorter than one second is merged into the previous one.
    assert plan_chunks(20.5, 10) == [(0, 10), (10, 10.5)]


def test_chunk_seconds_fit_the_upload_limit():
    assert chunk_seconds_for(24 * BYTES_PER_MB, 600) == 600
    # 1 MiB of 16 kHz mono PCM is about 32 seconds, minus the merge margin.
    assert chunk_seconds_for(BYTES_PER_MB, 600) == 31


class RecordingChunkWriter:
    def __init__(self, size=10):
        self.size = size
        self.calls = []

    def __call__(self, source, target, offset, duration):
        self.calls.append((offset, duration))
        target.write_bytes(b"c" * self.size)


def chunked_handler(segments_for_chunk, calls):
    def handler(request):
        index = len(calls)
        calls.append(request)
        return httpx.Response(200, json=segments_for_chunk(index))

    return handler


def test_timestamps_are_offset_once_per_chunk(audio_file, work_dir):
    calls = []
    writer = RecordingChunkWriter()

    def response(index):
        return verbose_json(
            segments=[{"start": 1.0, "end": 2.5, "text": f"chunk {index}"}],
            words=[{"word": "chunk", "start": 1.0, "end": 1.5}],
        )

    provider = make_provider(
        chunked_handler(response, calls),
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=writer,
        temp_dir=work_dir,
    )

    result = provider.transcribe(audio_file)

    assert writer.calls == [(0, 10), (10, 10), (20, 5)]
    assert len(calls) == 3
    # Each chunk is uploaded as a WAV file, never the original.
    assert all(b'filename="audio.wav"' in call.content for call in calls)
    assert all(b"c" * 10 in call.content for call in calls)
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (1.0, 2.5, "chunk 0"),
        (11.0, 12.5, "chunk 1"),
        (21.0, 22.5, "chunk 2"),
    ]
    assert [(w.start, w.end) for s in result.segments for w in s.words] == [
        (1.0, 1.5),
        (11.0, 11.5),
        (21.0, 21.5),
    ]
    assert result.language == "en"
    # Every temporary chunk was removed.
    assert list(work_dir.iterdir()) == []


def test_last_chunk_overshoot_is_clamped_to_the_total_duration(audio_file, work_dir):
    calls = []

    def response(index):
        # The last chunk is 5 s long; its segment overshoots by 0.4 s.
        end = 5.4 if index == 2 else 9.0
        return verbose_json(segments=[{"start": 4.0, "end": end, "text": "x"}])

    provider = make_provider(
        chunked_handler(response, calls),
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=RecordingChunkWriter(),
        temp_dir=work_dir,
    )

    result = provider.transcribe(audio_file)

    assert [(s.start, s.end) for s in result.segments] == [
        (4.0, 9.0),
        (14.0, 19.0),
        (24.0, 25.0),
    ]


def test_chunk_timestamps_past_the_chunk_end_are_rejected(audio_file, work_dir):
    calls = []

    def response(index):
        # 13 s is past the end of a 10 s chunk even with the tolerance.
        return verbose_json(segments=[{"start": 1.0, "end": 13.0, "text": "x"}])

    provider = make_provider(
        chunked_handler(response, calls),
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=RecordingChunkWriter(),
        temp_dir=work_dir,
    )

    with pytest.raises(InvalidTranscriptionResultError):
        provider.transcribe(audio_file)
    assert list(work_dir.iterdir()) == []


def test_temporary_chunks_are_removed_when_a_chunk_fails(audio_file, work_dir):
    calls = []
    writer = RecordingChunkWriter()

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(500)
        return httpx.Response(200, json=verbose_json())

    provider = make_provider(
        handler,
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=writer,
        temp_dir=work_dir,
    )

    with pytest.raises(TranscriptionProviderError):
        provider.transcribe(audio_file)

    # Nothing is reported for the chunks after the failure.
    assert len(calls) == 2
    assert len(writer.calls) == 2
    assert list(work_dir.iterdir()) == []


def test_chunk_larger_than_the_limit_fails_clearly(audio_file, work_dir):
    calls = []
    provider = make_provider(
        json_handler(verbose_json(), calls=calls),
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=RecordingChunkWriter(size=60),
        temp_dir=work_dir,
    )

    with pytest.raises(TranscriptionError) as excinfo:
        provider.transcribe(audio_file)

    assert "TRANSCRIPTION_CHUNK_SECONDS" in str(excinfo.value)
    assert calls == []
    assert list(work_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["media_error", "no_output"])
def test_chunking_failure_fails_clearly(audio_file, work_dir, failure):
    def writer(source, target, offset, duration):
        if failure == "media_error":
            target.write_bytes(b"partial")
            raise MediaError("ffmpeg failed", "/secret/path")

    provider = make_provider(
        json_handler(verbose_json()),
        duration_probe=fixed_duration(25.0),
        max_upload_bytes=50,
        chunk_seconds=10,
        chunk_writer=writer,
        temp_dir=work_dir,
    )

    with pytest.raises(TranscriptionError) as excinfo:
        provider.transcribe(audio_file)

    assert "/secret/path" not in str(excinfo.value)
    assert list(work_dir.iterdir()) == []


def test_ffmpeg_chunk_command(monkeypatch, tmp_path, audio_file):
    captured = {}

    def fake_run_ffmpeg(args, *, timeout=None, settings=None):
        captured["args"] = list(args)

    monkeypatch.setattr(ffmpeg_module, "run_ffmpeg", fake_run_ffmpeg)

    ffmpeg_write_chunk(audio_file, tmp_path / "chunk.wav", 600.0, 12.5)

    args = captured["args"]
    assert args[:2] == ["-protocol_whitelist", "file"]
    assert args[args.index("-ss") + 1] == "600.000"
    assert args[args.index("-t") + 1] == "12.500"
    # Seeking happens on the input, before -i.
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-i") + 1] == str(audio_file.resolve())
    assert args[args.index("-ar") + 1] == "16000"
    assert args[-1] == str(tmp_path / "chunk.wav")
