# ClipForge AI Backend

FastAPI backend for ClipForge AI.

## Requirements

- Python 3.11 or newer

## Setup

Run all commands from the `backend/` directory.

Create and activate a virtual environment:

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Optional: to use the local Whisper transcription provider, also install the
machine-learning extras (large; not needed for development or the tests):

```bash
pip install -r requirements-ml.txt
```

## Configuration

All backend settings are defined in one typed module,
`app/core/config.py`, as a `pydantic-settings` `Settings` class. Read them
through the cached accessor `get_settings()`; do not read configuration
environment variables directly elsewhere:

```python
from app.core.config import get_settings

settings = get_settings()
```

Settings are loaded from environment variables and from `backend/.env`.
Environment variables take precedence over values in the file. Settings are
loaded once per process; restart the server after changing them.

Create your local `.env` from the template:

```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

`.env` is ignored by Git. Never commit it or put real production secrets in
`.env.example`.

| Variable                      | Default                  | Description                                                  |
| ----------------------------- | ------------------------ | ------------------------------------------------------------ |
| `APP_ENV`                     | `development`            | `development`, `test`, or `production`.                      |
| `APP_NAME`                    | `ClipForge AI`           | Application name, used as the API title.                     |
| `DATABASE_URL`                | unset                    | PostgreSQL connection string (see below).                    |
| `REDIS_URL`                   | unset                    | Redis URL: Celery broker and result backend (see Background jobs). |
| `SECRET_KEY`                  | development-only default | Secret for signing tokens. Required in production.           |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30`                     | Access token lifetime in minutes (must be positive).         |
| `EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES` | `1440`       | Email verification token lifetime in minutes (positive).     |
| `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES` | `30`             | Password reset token lifetime in minutes (positive).         |
| `CORS_ORIGINS`                | empty                    | Comma-separated list of allowed origins (see below).         |
| `MAX_UPLOAD_SIZE_MB`          | `500`                    | Maximum upload size in megabytes (MiB, must be positive).    |
| `ALLOWED_VIDEO_EXTENSIONS`    | `.mp4,.mov,.webm,.mkv`   | Comma-separated upload extensions (see Video upload below).  |
| `ALLOWED_VIDEO_CONTENT_TYPES` | `video/mp4,video/quicktime,video/webm,video/x-matroska` | Comma-separated declared content types accepted for uploads. |
| `STORAGE_BACKEND`             | `local`                  | `local` or `s3` (see Storage below).                         |
| `LOCAL_STORAGE_DIR`           | `backend/storage`        | Root directory of the local storage backend.                 |
| `S3_ENDPOINT_URL`             | unset                    | S3 API URL for R2/MinIO; leave unset for AWS S3.             |
| `S3_REGION`                   | unset                    | Region (`auto` for R2). Required for `s3`.                   |
| `S3_BUCKET`                   | unset                    | Bucket name. Required for `s3`.                              |
| `S3_ACCESS_KEY_ID`            | unset                    | Access key ID. Required for `s3`.                            |
| `S3_SECRET_ACCESS_KEY`        | unset                    | Secret access key. Required for `s3`.                        |
| `S3_ADDRESSING_STYLE`         | `auto`                   | `auto`, `path` (MinIO), or `virtual`.                        |
| `EMAIL_PROVIDER`              | `fake`                   | `fake` (records, never sends; refused in production) or `smtp`. |
| `EMAIL_FROM`                  | unset                    | Sender address. Required for `smtp`.                         |
| `FRONTEND_URL`                | unset                    | Web app base URL used in emailed links. Required for `smtp`. |
| `SMTP_HOST`                   | unset                    | SMTP server. Required for `smtp`.                            |
| `SMTP_PORT`                   | `587`                    | SMTP port.                                                   |
| `SMTP_SECURITY`               | `starttls`               | `starttls`, `ssl`, or `none` (refused in production).        |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | unset                | SMTP credentials (both or neither).                          |
| `SMTP_TIMEOUT_SECONDS`        | `10`                     | SMTP connection/command timeout.                             |
| `MAX_VIDEO_DURATION_SECONDS`  | `7200`                   | Longest accepted video; longer ones are marked `failed`.     |
| `FFMPEG_PATH` / `FFPROBE_PATH` | `ffmpeg` / `ffprobe`    | FFmpeg binaries: a name on `PATH` or a full path.            |
| `FFMPEG_TIMEOUT_SECONDS`      | `1800`                   | Wall-clock limit of one ffmpeg run (at most 3600).           |
| `FFPROBE_TIMEOUT_SECONDS`     | `60`                     | Wall-clock limit of one ffprobe run (at most 600).           |
| `FFMPEG_MAX_MEMORY_MB`        | `8192`                   | Virtual memory limit of FFmpeg processes (Linux/macOS only). |
| `FFMPEG_MAX_OUTPUT_FILE_MB`   | `2048`                   | Largest file an FFmpeg process may write (Linux/macOS only). |
| `TRANSCRIPTION_PROVIDER`      | `fake`                   | `fake`, `openai_compatible` or `local_whisper` (see Transcripts below); `fake` is refused in production. |
| `TRANSCRIPTION_API_BASE_URL`  | unset                    | OpenAI-compatible API base URL. Required for `openai_compatible`. |
| `TRANSCRIPTION_API_KEY`       | unset                    | API key. Required for `openai_compatible` unless the URL is a loopback host. |
| `TRANSCRIPTION_MODEL`         | unset                    | Remote model name, or Whisper model size/directory. Required for both real providers. |
| `TRANSCRIPTION_TIMEOUT_SECONDS` | `300`                  | Wall-clock limit of one transcription request (at most 1800). |
| `TRANSCRIPTION_MAX_UPLOAD_MB` | `24`                     | Largest file sent in one request; larger audio is chunked.   |
| `TRANSCRIPTION_CHUNK_SECONDS` | `600`                    | Longest chunk (30-3600 s), further limited by the upload size. |

Invalid values (for example an unknown `APP_ENV` or `STORAGE_BACKEND`) stop
the application at startup with a validation error.

### Production secret

When `APP_ENV=production`, startup fails with a clear error if `SECRET_KEY` is
missing or empty, or if it is a known development default or placeholder (the
built-in default, the `.env.example` placeholder, or values such as
`change-me` or `secret`). Generate a strong value, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Set it in the production environment, never in a committed file.

## Database configuration

The backend uses PostgreSQL via SQLAlchemy 2.x and the `psycopg2` driver.
The connection string is the `DATABASE_URL` setting (environment variable or
`backend/.env`). Never commit real credentials to the repository.

URL format:

```
postgresql://<user>:<password>@<host>:<port>/<database>
```

Set it in `backend/.env`, or export it before starting the server:

```bash
# macOS / Linux
export DATABASE_URL="postgresql://<user>:<password>@localhost:5432/clipforge"

# Windows (PowerShell)
$env:DATABASE_URL = "postgresql://<user>:<password>@localhost:5432/clipforge"
```

To run PostgreSQL, Redis, and MinIO locally with Docker Compose, see
`docs/local-development.md`, which explains how to build `DATABASE_URL` from
the local PostgreSQL settings used by `docker-compose.yml` (note that the
Compose PostgreSQL service is published on host port `5433`). `.env.example` in
this directory lists the matching development-only backend values; never use
them in production.

The database layer lives in:

- `app/core/config.py` — `Settings.DATABASE_URL` and `get_database_url()`,
  which raises a clear error when it is not set.
- `app/database.py` — declarative `Base` for models, engine and session
  factory creation, and the `get_db` FastAPI dependency.

The engine is created lazily on first use, so the server starts and `/health`
responds even when `DATABASE_URL` is not set or PostgreSQL is not running.
Routes that need the database should depend on `get_db`, which provides a
session and closes it once the request finishes:

```python
from fastapi import Depends
from sqlalchemy.orm import Session

from app.database import get_db


@app.get("/example")
def example(db: Session = Depends(get_db)):
    ...
```

## Storage

Video files, audio and exports are stored through the `StorageBackend`
interface in `app/storage/base.py`. Obtain the configured backend with
`get_storage()` from `app.storage`; build keys with helpers such as
`video_source_key(user_id, video_id, ".mp4")`, which returns
`users/{user_id}/videos/{video_id}/source.mp4`.

- `save(key, fileobj, max_bytes=None)` streams the data in 1 MiB chunks to a
  temporary file and moves it into place when complete; it returns the number
  of bytes written. With `max_bytes`, larger data is rejected and nothing is
  stored.
- `open`, `exists`, `delete`, `size` work on stored objects; `delete` of a
  missing object is a no-op.
- `get_download_url(key, expires_seconds)` returns `None` when the API must
  stream the file itself (always the case for local storage).
- `local_path(key)` is a context manager yielding a local file path for
  FFmpeg (a temporary copy for remote backends). Treat it as read-only.

`STORAGE_BACKEND` selects `local` (default) or `s3` (see "S3-compatible
storage" below). Both backends apply the same key rules. The local backend keeps files
under `LOCAL_STORAGE_DIR` (default `backend/storage`, git-ignored). It rejects
keys that are empty, contain `..` or backslashes, are absolute, or contain
characters outside `A-Z a-z 0-9 . _ -` and `/`, have a segment ending in `.`
or a Windows device name (`CON`, `NUL`, `COM1`, ...), and it resolves every path
(following symlinks) and checks that it stays beneath the root before reading,
writing or deleting. The storage directory is never served as static files.

Limitations: the containment check happens before each file operation, so a
process that can already write inside the storage root could still race it
by swapping a directory for a symlink in between. Keep the storage directory
writable only by the backend.

### S3-compatible storage (AWS S3, Cloudflare R2, MinIO)

With `STORAGE_BACKEND=s3`, objects are stored in `S3_BUCKET` through `boto3`
(`app/storage/s3.py`). `get_storage()` raises `StorageConfigurationError`
naming every missing setting when `S3_BUCKET`, `S3_REGION`,
`S3_ACCESS_KEY_ID` or `S3_SECRET_ACCESS_KEY` is unset; the application still
starts, so only storage use fails.

- `save` uploads with `upload_fileobj` (multipart above 8 MiB); `max_bytes`
  is enforced while streaming and a failed multipart upload is aborted.
- `get_download_url(key, expires_seconds)` returns a presigned GET URL valid
  for `expires_seconds` (1 second to 7 days).
- `local_path(key)` downloads to a temporary file that is deleted when the
  context exits, even on error.
- `exists` returns `False` only when the provider confirms the object is
  missing (HTTP 404 with `404`/`NoSuchKey`/`NotFound`). Permission,
  authentication, timeout and server errors are raised, never reported as
  "missing" or as success. `open`/`size` raise `StorageObjectNotFoundError`
  for a missing object.
- Requests use a 10 s connect timeout, a 60 s read timeout and at most 3
  attempts in total, including the first request (botocore "standard"
  retries with `total_max_attempts=3`).

Example configuration (placeholders only; put real values in `backend/.env`
or the deployment environment, never in committed files). See
`docs/setup/s3-storage.md` for how to obtain them.

Cloudflare R2:

```bash
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=https://<your-cloudflare-account-id>.r2.cloudflarestorage.com
S3_REGION=auto
S3_BUCKET=<your-r2-bucket-name>
S3_ACCESS_KEY_ID=<your-r2-access-key-id>
S3_SECRET_ACCESS_KEY=<your-r2-secret-access-key>
S3_ADDRESSING_STYLE=path
```

Local MinIO (Docker Compose; development-only values from
`backend/.env.example`; create the `clipforge` bucket in the MinIO console at
`http://localhost:9001` first):

```bash
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=http://localhost:9000
S3_REGION=us-east-1
S3_BUCKET=clipforge
S3_ACCESS_KEY_ID=clipforge_dev
S3_SECRET_ACCESS_KEY=clipforge_dev_password
S3_ADDRESSING_STYLE=path
```

For AWS S3, leave `S3_ENDPOINT_URL` unset and set `S3_REGION` to the bucket's
region.

Limitations: a `HEAD` request has no error body, so a missing bucket that
answers with a bare `404` is indistinguishable from a missing object in
`exists`, which then returns `False` for what is really a configuration
error. Aborting a multipart upload that fails part-way (for example when
`max_bytes` is exceeded) is done by boto3's transfer manager; the tests check
that the size limit stops the upload but do not exercise a real multipart
abort. Bucket creation, lifecycle rules and CORS are not managed by the
backend.

## Database migrations

Schema changes are managed with [Alembic](https://alembic.sqlalchemy.org/).
The configuration is `alembic.ini`, and the migration scripts live in
`migrations/versions/`. `migrations/env.py` reads the database URL from the
shared settings (`DATABASE_URL`, via `get_database_url()`) and uses
`Base.metadata` from `app/database.py`, with every model registered by
`app/models/__init__.py`. No URL or credential is stored in `alembic.ini`.

Run these commands from the `backend/` directory with `DATABASE_URL` set
(environment variable or `backend/.env`):

```bash
# Apply all migrations
alembic upgrade head

# Show the current revision and the history
alembic current
alembic history

# Undo the most recent migration
alembic downgrade -1

# Create a new migration after changing a model (review the generated file!)
alembic revision --autogenerate -m "describe the change"
```

When you add a model, import it in `app/models/__init__.py` so autogenerate
sees it.

## Users and password hashing

- `app/models/user.py` — the `User` model (`users` table). Emails are
  stripped and lowercased before they are stored, and the database enforces
  this with a `CHECK (email = lower(email))` constraint plus a unique index on
  `email`, so uniqueness is case-insensitive at the database level.
- `app/repositories/users.py` — `get_by_email`, `get_by_id`, and
  `create_user`. `create_user` hashes the password, commits the user, and
  raises `DuplicateEmailError` if the email is already registered (in any
  letter case) or `ValueError` for an invalid email or empty password.
  `update_full_name` and `set_password` commit profile changes;
  `set_password` also increments `token_version`.
- `app/core/security.py` — `hash_password` and `verify_password`.

Passwords are hashed with **Argon2id** through `argon2-cffi`, using the
library's default (RFC 9106 based) parameters. Argon2id won the Password
Hashing Competition and is the first choice in the OWASP Password Storage
Cheat Sheet; it is memory-hard, which makes GPU/ASIC cracking far more
expensive than with bcrypt, and it has no 72-byte password length limit.
Password hashes must never be logged or returned by the API; `User.__repr__`
only shows the id.

Email validation is intentionally basic (a non-empty local part and domain
separated by `@`, no whitespace, at most 320 characters); stricter
validation belongs to the request schemas that will use these helpers.

## Videos and processing status

`app/models/video.py` defines the `Video` model (`videos` table, migration
`0002`). Each video belongs to a user (`user_id`, indexed, `ON DELETE
CASCADE`) and stores the upload's `original_filename`, `content_type`,
`size_bytes` (must be `>= 0`) and `storage_key`. `duration_seconds`, `width`,
`height` and `fps` are nullable and filled in once the video is probed.

`status` is one of `uploaded` (default), `processing`, `ready` or `failed`,
stored as a string with a `CHECK` constraint. Change it only with
`transition_status(video, new_status, error=None)`, which holds the single
table of allowed transitions (`ALLOWED_TRANSITIONS`):

| From         | To           | Meaning     |
| ------------ | ------------ | ----------- |
| `uploaded`   | `processing` | start       |
| `processing` | `ready`      | success     |
| `processing` | `failed`     | failure     |
| `failed`     | `processing` | retry       |
| `ready`      | `processing` | reprocess   |

Any other change, including to the same status, raises
`InvalidStatusTransitionError` and leaves the video unchanged. Moving to
`failed` stores `error` in `error_message` (a generic message if it is missing
or blank); every other transition clears `error_message`, and passing `error`
to one raises `ValueError`. The helper does not commit; the caller does. Each
transition is logged as `video_status_changed` with the video id and the old
and new status (never the error text).

`app/schemas/video.py` provides the `VideoRead` and `VideoList` response
schemas. `VideoRead` deliberately omits the internal `storage_key`.

Limitation: `transition_status` checks the status held in memory, so two
workers changing the same video concurrently can both succeed; callers that
need this guarantee must lock the row (for example `SELECT ... FOR UPDATE`).

## Transcripts

`app/models/transcript.py` defines how transcripts are stored (migration
`0006`):

- `transcripts`: `id`, `video_id` (`ON DELETE CASCADE`, unique index
  `ix_transcripts_video_id`, so a video has at most one transcript and it
  belongs to the video's owner), `language`, `requested_language`,
  `provider`, `model`, `full_text`, `created_at`.
- `transcript_segments`: `id`, `transcript_id` (`ON DELETE CASCADE`),
  `index`, `start_seconds`, `end_seconds`, `text`, and `words` (a JSON list
  of `{word, start, end}`, or `NULL` when no word timings were reported).
  A unique constraint on `(transcript_id, index)` keeps the order unique and
  is the index used to load segments; `CHECK` constraints require
  `index >= 0`, `start_seconds >= 0` and `end_seconds > start_seconds`.
  `Transcript.segments` is always ordered by `index`.

Deleting a video (or its user) deletes its transcript and segments;
`delete_video` also deletes them explicitly for databases that do not enforce
foreign keys.

**Languages.** `language` is the language the provider reported and
`requested_language` the one the user asked for; `NULL` means unknown
(respectively automatic detection). Both are ISO 639 codes with optional
BCP 47 subtags (`en`, `te`, `hi-IN`), validated by
`normalize_language()` in `app/transcription/base.py`, which accepts
`None`/`""`/`auto` for automatic detection and keeps `und` as an explicit
"unknown". Codes are only trimmed and their primary code lowercased; a
transcript is never relabeled with another language. Text is stored as given,
so non-Latin scripts and code-switched text keep their original form.

**Providers** (`app/transcription/`):

- `base.py` — the `TranscriptionProvider` protocol
  (`transcribe(audio_path, language=None) -> TranscriptionResult`) and the
  frozen result dataclasses `TranscriptionResult` (language, segments,
  provider, model, `full_text`), `TranscriptionSegment` (start, end, text,
  optional words) and `WordTiming`. They validate on creation: timestamps
  must be finite and non-negative, segments must end after they start and be
  in chronological order, words must not end before they start.
  `check_within_duration()` rejects timestamps past the end of the audio.
  Failures raise `TranscriptionError` subclasses.
- `fake.py` — `FakeTranscriptionProvider`, for development and tests. It
  makes no network calls; it reads the duration of a WAV file (other files
  are treated as 16 kHz 16-bit mono PCM, so the duration follows from the
  size) and returns 5-second segments covering it, with placeholder text and
  evenly spaced word timings. The same file always gives the same result;
  an empty or silent (< 1 ms) file gives zero segments.
- `openai_compatible.py` — `OpenAICompatibleTranscriptionProvider`, which
  posts the audio to `<TRANSCRIPTION_API_BASE_URL>/audio/transcriptions`
  (multipart, `model`, `response_format=verbose_json`,
  `timestamp_granularities[]=segment` and `word`, and `language` when one
  was requested) with `Authorization: Bearer <TRANSCRIPTION_API_KEY>`.
  See "Real transcription providers" below.
- `local_whisper.py` — `LocalWhisperTranscriptionProvider`, which runs
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) in the worker
  process with word timestamps.
- `mapping.py` — shared helpers that validate provider output, map Whisper
  language names (`english`) to ISO codes (`en`), and place timestamps on
  the timeline of the whole file.
- `get_transcription_provider()` (in `app/transcription/__init__.py`)
  returns the provider named by `TRANSCRIPTION_PROVIDER` (default `fake`).
  An unknown name does not stop the app from starting but raises
  `TranscriptionConfigurationError` when transcription is used; so does
  `fake` when `APP_ENV=production`, a real provider whose required settings
  are missing or invalid (the message names every missing setting, never a
  value), and `local_whisper` when faster-whisper is not installed.

### Real transcription providers

Both are optional; development and the tests use `fake` and need no account,
key or ML package. Setup steps are in `docs/setup/transcription.md`.
Example configuration (placeholders only; put real values in `backend/.env`
or the deployment environment, never in committed files):

```bash
# A hosted OpenAI-compatible API
TRANSCRIPTION_PROVIDER=openai_compatible
TRANSCRIPTION_API_BASE_URL=<your-transcription-api-base-url>
TRANSCRIPTION_API_KEY=<your-transcription-api-key>
TRANSCRIPTION_MODEL=<your-transcription-model>

# Local Whisper (after pip install -r requirements-ml.txt)
TRANSCRIPTION_PROVIDER=local_whisper
TRANSCRIPTION_MODEL=small
```

| | `openai_compatible` | `local_whisper` |
| --- | --- | --- |
| Install | nothing extra (`httpx` is a base dependency) | `requirements-ml.txt` (faster-whisper, CTranslate2; hundreds of MB) plus model files |
| Account / key | yes, for a hosted API | none |
| Privacy | audio is sent to the provider | audio never leaves the worker (the model may be downloaded on first use) |
| Speed / resources | provider's hardware; per-minute pricing and rate limits | worker CPU/GPU and memory; slow on CPU for large models |
| Limits | upload size (chunked here), timeouts, rate limits | none besides the job time limit |

**openai_compatible details.**

- `TRANSCRIPTION_API_BASE_URL` must be `https://`; `http://` is accepted only
  for a loopback host (`localhost`, `127.0.0.1`, `::1`), e.g. a self-hosted
  Whisper server, for which `TRANSCRIPTION_API_KEY` may also be left unset.
  URLs with credentials, a query or a fragment are rejected. Redirects are
  not followed, so the key is only ever sent to the configured host.
- Requests have a `TRANSCRIPTION_TIMEOUT_SECONDS` timeout (10 s to connect).
  They are not retried by the provider; errors raise
  `TranscriptionProviderError` with `status_code`, `retryable` and
  `retry_after`: `401`/`403` (bad key), `413` and other `4xx` are not
  retryable; `429` (with `Retry-After`), `408`, `5xx`, network errors and
  timeouts (`TranscriptionTimeoutError`) are. Response bodies are never
  included in errors or logs.
- **Chunking.** The duration is read with ffprobe first. A file larger than
  `TRANSCRIPTION_MAX_UPLOAD_MB` is cut with FFmpeg into consecutive,
  non-overlapping chunks of mono 16 kHz WAV (`-ss <offset> -t <duration>`).
  A chunk lasts at most `TRANSCRIPTION_CHUNK_SECONDS`, and at most as long as
  fits in the upload limit (32,000 bytes per second plus a header margin);
  a last piece under 1 second is merged into the previous chunk. Each
  chunk's timestamps are checked against that chunk and then shifted by the
  chunk's start exactly once. A chunk that still exceeds the limit fails
  the transcription with a clear error. Chunks live in a temporary
  directory that is deleted afterwards, on success and on failure, and each
  chunk file is deleted as soon as it has been sent.
- **Validation.** Responses must be JSON objects with numeric, finite,
  non-negative times and string texts; anything else raises
  `InvalidTranscriptionResultError`. A segment or word may end up to 1
  second past the end of its audio (it is clamped); more is rejected.
  Transcript-level words (OpenAI) are attached to the segment containing
  their midpoint; words are clamped into their segment. A response with text
  but no segments becomes one segment covering the audio. The final result
  is checked against the file's duration.
- The detected language is mapped to an ISO 639 code; a requested language
  is sent as its primary subtag (`hi-IN` → `hi`) and reported as requested.
- Logs: `transcription_completed` (provider, model, language, segment and
  chunk counts, duration) and `transcription_request_failed` (chunk index,
  status, retryable, reason). Never the key, the audio, or transcript text.

**local_whisper details.** faster-whisper is imported only when the provider
is created and the model is loaded on the first transcription (then reused).
Without the package, `get_transcription_provider()` raises
`TranscriptionConfigurationError` naming `requirements-ml.txt`. It runs with
`device="auto"` and `compute_type="default"`; its timestamps get the same
validation as the remote provider, using the duration faster-whisper
reports. Exceptions from the library are reported only by type.

`app/repositories/transcripts.py` provides `get_transcript_for_video` and
`replace_transcript(db, video_id, result, requested_language=None)`, which
stores a result as the video's transcript, replacing any earlier one so a
retried job never creates duplicates. It flushes but does not commit, and
logs `transcript_saved` (ids, provider, language, segment count; never the
text).

Limitations: the real providers are tested only with mocks
(`httpx.MockTransport` and a fake Whisper model), never against a real
service or model. Chunks do not
overlap, so a word spoken across a chunk boundary may be cut or split; the
language detected for the first chunk is reported for the whole file.
Chunking needs FFmpeg/ffprobe on the worker (the duration is always probed
with ffprobe). `http://` is accepted only for loopback hosts, so a
self-hosted server on another machine or container needs HTTPS. The local
Whisper device and compute type are not configurable yet. No claims are made
about transcription quality for regional languages (Telugu, Hindi, Tamil,
Kannada, Malayalam); the tests cover storage of their scripts, not
recognition. Segments may overlap if a provider reports them that way; only
their start times must be in order.

### Transcription jobs

`POST /api/v1/videos/{id}/transcription` (body `{"language": "en"}`, or no
body / `null` / `"auto"` for automatic detection) queues the `transcription`
job `app.worker.transcription.transcribe_video` and returns `202
{job_id, status}`; poll `GET /api/v1/jobs/{job_id}`. It returns `409
video_not_ready` unless the video is `ready`, `409 transcription_in_progress`
while a transcription job for the video is queued or running, `422` for an
invalid language (see `normalize_language`) or unknown field, and `503
queue_unavailable` when the job cannot be published. The job payload is
`{"video_id", "language"}`, the idempotency key is the video id (so even two
simultaneous requests share one job), 3 attempts, soft/hard time limit
3300/3420 s.

The job (`run_transcription`):

1. checks that the video exists and is `ready` (the video's status is never
   changed by transcription; the job records success or failure);
2. extracts the audio with `extract_audio` (16 kHz mono WAV) into a new
   temporary directory, from a local path obtained through
   `storage.local_path` (the key must be the video's own key);
3. calls the provider selected by `TRANSCRIPTION_PROVIDER`;
4. checks the result against the video's duration (timestamps up to 1 s past
   the end are clamped to the duration, later ones fail the job as an
   invalid result), removes NUL characters, truncates the provider/model
   names to their column sizes, and replaces the transcript and its segments
   with `replace_transcript` in **one transaction**, after re-checking (with
   the row locked) that the video still exists and is `ready`.

The temporary directory is removed whether the job succeeds, fails or times
out. Until step 4 commits, the previous transcript is untouched, so a failed,
timed-out or cancelled job never leaves a partial or missing transcript; a
failure while saving is rolled back. A job cancelled while running does not
save its result.

Retries: storage read errors and provider errors (`TranscriptionError`) are
treated as temporary and retried with the standard backoff; after the last
attempt the job fails with "The transcription service failed. Try again
later.". An invalid result, a missing or unreadable audio track ("The video's
audio could not be extracted. It may have no audio track."), a missing FFmpeg,
an audio-extraction timeout, an unknown or refused provider, a missing file
and a video that is no longer ready fail at once. Job messages never contain
paths, keys, tool or provider output, or transcript text. Log events:
`transcription_requested`, `transcription_rejected`, `transcription_started`,
`transcript_saved`, `transcription_completed` (ids, provider, model,
language, segment count) and failure events with the error type only.

### Reading transcripts and subtitles

All of these require authentication and use `get_owned_video` (another
user's video is `404`); a video without a transcript is `404
transcript_not_found`. Responses carry `Cache-Control: private, no-store`
where they are downloads.

- `GET /videos/{id}/transcript` — the transcript (`id`, `video_id`,
  `language`, `requested_language`, `provider`, `model`, `full_text`,
  `created_at`, `timing_granularity`) with its `segments` in order (`id`,
  `index`, `start_seconds`, `end_seconds`, `text`, `words` or `null`).
  `timing_granularity` is `"word"` only when every segment has word timings,
  otherwise `"segment"`: with segment-level providers only segment
  boundaries can be selected precisely.
- `GET /videos/{id}/transcript/search?q=...&limit=50` — segments whose text
  contains `q` (case-insensitive, whitespace collapsed; 1-200 characters;
  `limit` 1-500), as `{query, total, items}`. Each item is a full segment, so
  its `id`, `index` and times can be used to select ranges.
- `GET /videos/{id}/transcript.srt` / `transcript.vtt` — subtitles generated
  by the pure functions in `app/subtitles/formats.py`, served as
  `application/x-subrip; charset=utf-8` / `text/vtt; charset=utf-8` with
  `Content-Disposition: attachment; filename="<video name>.srt"` (only
  `A-Z a-z 0-9 . _ -` are kept from the video's file name; `transcript.srt`
  if nothing is left). SRT uses `HH:MM:SS,mmm` and cues numbered from 1;
  WebVTT starts with `WEBVTT` and uses `HH:MM:SS.mmm`. Cues are in start
  order with the stored times (rounded to milliseconds); empty cues are
  skipped, line breaks and control characters in the text are collapsed or
  removed, long captions are wrapped at 42 characters per line, WebVTT text
  is escaped (`&`, `<`, `>`) and `-->` is written as `->` in SRT. Overlapping
  segments are kept overlapping (both formats allow it).

All times are seconds of **source-video** time; nothing is converted to clip
time yet, so clip-relative views must subtract the clip start from these
values. Segment `id`s are stable for the life of a transcript, but a new
transcription replaces the segments and gives them new ids (use `index` and
the times to map between versions).

Limitations: search is a substring match over the stored segment texts (no
stemming or ranking) done in the API process, which is fine for the
transcript sizes the duration limit allows. The `openai_compatible`
provider limits each request with `TRANSCRIPTION_TIMEOUT_SECONDS`; the
`local_whisper` provider has no timeout of its own beyond the job's time
limits. A cancelled job's result can still be saved if the cancellation
arrives after the final cancellation check.

## Video upload

`POST /api/v1/videos` (`app/api/v1/videos.py`) uploads a long-form video for
the authenticated user. It requires authentication (`get_current_user`,
including the CSRF check for cookie-authenticated requests), and the
credentials are checked before any of the request body is read.

- **Request**: `multipart/form-data` with exactly one file part named
  `file` and no other fields.
- **Size**: a `Content-Length` above `MAX_UPLOAD_SIZE_MB` (plus 64 KiB for
  the multipart framing) is rejected with `413` before the body is read;
  otherwise reading stops with `413` as soon as the streamed body passes that
  limit, and the file itself is held to the exact limit when it is written to
  storage. The file part is spooled to a temporary file (at most 1 MiB in
  memory), so an upload is never loaded into memory in full. Empty files are
  rejected with `422`.
- **Type**: the extension must be in `ALLOWED_VIDEO_EXTENSIONS`, the declared
  content type in `ALLOWED_VIDEO_CONTENT_TYPES`, and both must belong to the
  same container family as the file's first bytes: an ISO BMFF `ftyp` box
  for `.mp4`/`.mov`, the EBML header (`1A 45 DF A3`) for `.webm`/`.mkv`.
  Anything else is `415`. The stored `content_type` is derived from the
  validated extension, never copied from the client. This is format
  validation, not malware scanning. Helpers live in
  `app/services/video_upload.py`.
- **File name**: `original_filename` is sanitized for display only:
  directory components (`/` and `\`), control and formatting characters and
  surrounding whitespace are removed and it is limited to 255 characters,
  keeping the extension. It is never used in a storage key, a path or a
  command.
- **Storage**: the file is streamed to storage under
  `video_source_key(user_id, video_id, ext)` with a newly generated video id,
  and a `Video` row is created with status `uploaded`; then a metadata
  extraction job is queued (see "Media metadata extraction") and the response
  is `201` with `VideoRead` (normally still `uploaded`). If the job cannot be
  queued (Redis down, `REDIS_URL` unset) the upload still succeeds, but the
  video is returned as `failed` with "Processing could not be started. Try
  again later."
- **Cleanup**: if writing to storage or committing the row fails, the stored
  object is deleted and `500 upload_failed` is returned. Oversized or invalid
  uploads never reach storage, and an interrupted request ends with `400
  upload_interrupted` before anything is stored; the temporary files spooled
  so far are closed (and so deleted) straight away. A misconfigured storage
  backend returns `503`.
- **Logging**: `video_uploaded` (user id, video id, size, content type) and
  `video_upload_rejected` (user id, reason, size, extension and declared
  content type when known). An extension or content type that is not a
  recognized video type is logged as `other`. File names and contents are
  never logged.

`GET /api/v1/videos/upload-limits` (authenticated) returns the limits this
endpoint enforces, so clients can check a file before sending it without
keeping their own copy: `max_upload_size_bytes` (`MAX_UPLOAD_SIZE_MB` in
bytes) and the entries of `ALLOWED_VIDEO_EXTENSIONS` and
`ALLOWED_VIDEO_CONTENT_TYPES` that the upload actually accepts (those with a
known file signature). The upload itself still validates everything.

Limitations: no resumable or chunked uploads; the whole file is first
spooled to a temporary file on the API server's disk and then copied to
storage, so the server needs free temporary space of up to
`MAX_UPLOAD_SIZE_MB` per concurrent upload. Only the first bytes are checked,
so a file with a valid header but corrupt contents is accepted; the metadata
job then marks it `failed`. Older QuickTime files that do not start with an
`ftyp` box are rejected. If deleting a stored
object fails during cleanup, the failure is logged
(`video_upload_cleanup_failed`) and the object is left behind.

## Managing videos

`app/api/v1/videos.py` also lists, reads, deletes and serves the current
user's videos. Every endpoint requires authentication. Ownership is checked
by the reusable dependency `get_owned_video` (`app/api/deps.py`), which loads
the video by id **and** owner (`app/repositories/videos.py`) and returns the
same `404 not_found` for another user's video as for a missing one, so a
video's existence is never revealed. Storage keys are never returned.

- **List** (`GET /videos`): `limit` (default 20, 1-100), `offset` (0 to
  1,000,000) and an optional `status` filter; ordered by `created_at`, newest
  first (ties broken by id). `total` counts all matching videos.
- **Delete** (`DELETE /videos/{id}`): in one transaction the video row (again
  scoped to the owner), its processing jobs and a `pending_storage_deletions`
  row for its file (migration `0005`) are written; only then is the file
  deleted. If that fails, the video is still gone (`204`), the failure is
  logged as `storage_deletion_failed` (deletion id, attempts, error type; never
  the key or exception text) and the pending row stays. If the database
  transaction fails nothing is deleted and `500 delete_failed` is returned.
- **Cleanup outbox**: `app/services/storage_cleanup.py` retries pending
  deletions (fewest attempts first, 50 per run) and removes each row once its
  object is gone. Deleting a missing object succeeds, so retries are
  idempotent. The Celery beat task
  `app.worker.storage_cleanup.retry_storage_deletions` runs it every 10
  minutes; from the 10th failed attempt on, failures are logged as errors.
- **Media** (`GET /videos/{id}/media`): if the storage backend issues download
  URLs (S3), the response is a `307` redirect to a presigned URL valid for 5
  minutes (`MEDIA_URL_EXPIRE_SECONDS`), only ever issued after the ownership
  check. Otherwise (local storage) the API streams the file in 64 KiB chunks
  with `Content-Type` from the stored (validated) content type,
  `Accept-Ranges: bytes`, `Content-Length`, `Cache-Control: private,
  no-store` and `X-Content-Type-Options: nosniff`. A single `Range:
  bytes=a-b`, `bytes=a-` or `bytes=-n` returns `206` with `Content-Range`;
  only the requested bytes are read. Malformed ranges, units other than
  `bytes`, and ranges that select no byte (start at or past the end,
  `bytes=-0`, any range of an empty file) return `416` with
  `Content-Range: bytes */<size>`. A request for several ranges, or one with
  `If-Range`, gets the whole file (`200`), as RFC 9110 allows. A row whose
  file is missing returns `404 media_not_found`.

Limitations: presigned URLs are bearer URLs — anyone holding one can download
the file until it expires (5 minutes), and they contain the object key. S3
serves the object with the content type stored at upload (the upload does
not set one yet), not the video's `content_type`. Multipart (`multipart/
byteranges`) responses are not supported. A concurrent retry run and a
request can both delete the same object, which is harmless.

## Background jobs

Long-running work (metadata extraction, transcription, clip detection,
rendering) runs in a [Celery](https://docs.celeryq.dev/) worker with Redis
(`REDIS_URL`) as broker and result backend. Every job also has a
`ProcessingJob` row (`processing_jobs` table, migration `0004`), which is the
source of truth for its status.

Run the worker (and, for stale-job recovery, the scheduler) from the
`backend/` directory with the same settings as the API (`DATABASE_URL`,
`REDIS_URL`):

```bash
celery -A app.worker.celery_app worker --loglevel=INFO
celery -A app.worker.celery_app beat --loglevel=INFO
```

With `APP_ENV=test`, tasks run eagerly in the calling process
(`task_always_eager`), so the tests need no Redis.

### Job model

- `job_type`: `metadata`, `transcription`, `clip_detection` or `render`.
- `status`: `queued` → `running` → `succeeded` or `failed`; a retryable
  failure goes back to `queued`; queued and running jobs can be `cancelled`.
  A failed job is never reported as succeeded.
- `user_id` / `video_id` (nullable, `ON DELETE CASCADE`), `payload` (a JSON
  object of at most 16 KiB), `celery_task_id`, `attempts` / `max_attempts`,
  `idempotency_key`, `request_id`, `error_message`.
- `created_at` (queued), `started_at` (latest attempt) and `finished_at`, so
  queue wait (`started_at - created_at`) and run time
  (`finished_at - started_at`) can be measured per job type. Both are also
  logged as `queue_wait_ms` and `duration_ms`.

### Writing and queuing jobs (`app/worker/jobs.py`)

```python
from app.models.processing_job import JobType
from app.worker.jobs import JobContext, JobError, RetryableJobError, enqueue_job, job_task


@job_task(JobType.METADATA, max_attempts=3, soft_time_limit=600, time_limit=660)
def extract_metadata(ctx: JobContext) -> None:
    video_id = ctx.payload["video_id"]
    ...  # raise JobError("...") or RetryableJobError("...") on failure


job = enqueue_job(
    JobType.METADATA,
    extract_metadata,
    db=db,
    user_id=user.id,
    video_id=video.id,
    payload={"video_id": str(video.id)},
    idempotency_key=str(video.id),
)
```

- `enqueue_job` validates the job type, task and payload, commits the job
  row (with its Celery task id) **before** publishing, then publishes. If
  publishing fails (Redis down, or `REDIS_URL` unset) the job is marked
  `failed` with "The job could not be queued. Try again later." and
  `JobDispatchError` is raised; it is never left queued.
- **Idempotency**: while a job with the same type and `idempotency_key` is
  queued or running, `enqueue_job` returns that job instead of creating
  another (enforced by a unique index on `active_key`, which is cleared when
  the job finishes).
- **State changes are atomic**: each transition is a conditional `UPDATE`
  (`WHERE id = ... AND status = ...`). A worker only runs a job it moved from
  `queued` to `running`, so a duplicate or late message (for example for a
  cancelled or finished job) does nothing.
- **Errors**: `error_message` holds the message of a `JobError` (credentials
  in URLs and control characters removed, truncated to 500 characters), "The
  job timed out." for a soft time limit, or "The job failed unexpectedly
  (ExceptionType)." for any other exception. Exception text and stack traces
  are never stored.
- **Timeouts**: every job task has a soft time limit (default 600 s, raises
  inside the task and fails the job) and a hard time limit (default 660 s,
  at most 3600 s, kills the worker process).
- **Retries**: only `RetryableJobError` is retried, until `max_attempts`
  (default 3, at most 10) attempts were made, after
  `10 s * 2^(attempt - 1)` (capped at 600 s, no jitter). Each attempt
  increments `attempts`. Retries run the same function with the same payload,
  so tasks must write their results idempotently (keyed by job or operation,
  overwritten rather than appended). If a retry cannot be published the job
  fails.
- **Cancellation**: `cancel_job(db, job_id, user_id=...)` cancels a queued or
  running job owned by that user. A queued job is skipped when its message
  arrives; a running task is not interrupted, but its result is discarded and
  it can stop early by checking `ctx.is_cancelled()`.
- **Lost workers**: tasks are acknowledged late (`task_acks_late`). A job
  still `running` more than 65 minutes (the maximum hard time limit plus 5
  minutes) after it started is marked `failed` ("The job was interrupted ...")
  by `fail_stale_jobs`, which runs when a worker starts and every 5 minutes
  through Celery beat.
- **Request IDs**: every API request gets an `X-Request-ID` (the client's
  value if it is 1-64 characters of `A-Z a-z 0-9 . _ -`, otherwise a random
  one), returned in the response header. `enqueue_job` stores it on the job,
  and the job's log events (`job_queued`, `job_started`, `job_succeeded`,
  `job_failed`, `job_retry_scheduled`, ...) carry `job_id`, `job_type`,
  `request_id` and `attempt`, never the payload or exception text.
- Broker connections and Redis sockets time out after 5 s; publishing is
  retried at most 3 times.

`GET /api/v1/jobs/{id}` returns a job's status to its owner (see Endpoints).

Limitations: Celery itself logs task exceptions (including their text) in
the worker log, so keep secrets out of exception messages and protect worker
logs. A queued job whose message is lost inside Redis (for example after
Redis data loss) stays queued; there is no automatic re-publishing. Stale
running jobs are failed, not retried. A running task is not forcibly stopped
on cancellation. Jobs are queued by the upload endpoint (metadata
extraction) and `POST /videos/{id}/transcription`; no API endpoint cancels
them yet.

## Media metadata extraction

FFmpeg and ffprobe must be installed on the machine running the Celery
worker (the API itself never runs them). Install them from your package
manager (`apt install ffmpeg`, `brew install ffmpeg`, or the Windows builds
linked from <https://ffmpeg.org/download.html>) and, if they are not on
`PATH`, set `FFMPEG_PATH` and `FFPROBE_PATH` to the binaries.

`app/media/ffmpeg.py` wraps the tools (no Python FFmpeg library is used):

- `probe(path) -> MediaInfo`: duration, width, height, fps, `has_video`,
  `has_audio`, video and audio codec, from `ffprobe -print_format json`.
  Values ffprobe does not report or reports as nonsense (zero, negative,
  infinite, absurd frame rates) are `None`; cover art is not counted as a
  video stream.
- `extract_audio(src, dst, sample_rate=16000)`: writes mono 16-bit PCM WAV;
  a partial file is removed on failure.
- `run_ffmpeg(args)`: runs `ffmpeg -nostdin -hide_banner -loglevel error
  <args>`.

Processes are started with an argument list and `shell=False` (a string
command line is rejected), stdin closed, and a timeout
(`FFMPEG_TIMEOUT_SECONDS` / `FFPROBE_TIMEOUT_SECONDS`) after which they are
killed. stdout and stderr go to temporary files and only a bounded amount is
read back (1 MiB of stdout, the last 8 KiB of stderr). On Linux/macOS the
child also gets `RLIMIT_AS` (`FFMPEG_MAX_MEMORY_MB`), `RLIMIT_FSIZE`
(`FFMPEG_MAX_OUTPUT_FILE_MB`) and no core dumps. Inputs are passed as
absolute paths of existing regular files (so they can never be taken for an
option) with `-protocol_whitelist file`, so a crafted file such as an HLS
playlist cannot make FFmpeg open URLs. Failures raise `MediaError`, whose
message includes the truncated stderr tail (at most 1000 characters, control
characters removed); `MediaTimeoutError` and `FFmpegNotFoundError` (binary
missing; the message names the setting to fix) are subclasses.

After an upload, the endpoint queues the `metadata` job
`app.worker.metadata.extract_metadata` (payload `{"video_id": ...}`,
idempotency key = video id, 3 attempts, soft/hard time limit 900/960 s). It:

1. moves the video to `processing` (from `uploaded`, `failed` or `ready`;
   the row is locked with `SELECT ... FOR UPDATE`);
2. checks that `storage_key` is the video's own key
   (`users/<user_id>/videos/<video_id>/...`), gets a local path through
   `storage.local_path(key)` and checks that the resolved path is a regular
   file inside the storage root (local backend) or the temporary directory
   (remote backends, which download a copy);
3. probes it and saves `duration_seconds`, `width`, `height` and `fps`, then
   moves the video to `ready`.

The video is moved to `failed` with a message for the owner (the job fails
with the same message) when the file is not a valid video, has no video
stream, has no usable duration or dimensions, is longer than
`MAX_VIDEO_DURATION_SECONDS` ("The video is too long (N seconds); the maximum
is M seconds."), is missing, is stored somewhere unexpected, when ffprobe is
not installed or times out, and on any unexpected error or time limit. Tool
output, paths and keys are never stored in these messages or logged. Only an
I/O or storage error while reading the file is retried; the video stays
`processing` while a retry is queued and fails on the last attempt. A failed
video's metadata fields are cleared. Running the job again for a video
re-probes the file and overwrites the fields, so retries and reprocessing
never duplicate anything. Log events: `video_metadata_started`,
`video_metadata_extracted` (duration, dimensions, fps, codecs),
`video_metadata_rejected` (reason), `video_metadata_queue_failed` and others,
all with `video_id` only.

Limitations: `has_audio` and the codecs are logged but not stored (the
`videos` table has no columns for them yet). The resource limits are not
applied on Windows, where only the timeout and the bounded output capture
protect the worker. `RLIMIT_AS` limits virtual memory, so a very low
`FFMPEG_MAX_MEMORY_MB` can make FFmpeg fail on machines with many cores.
There is no endpoint yet to retry processing of a failed video. When a
worker is killed during a metadata job (hard time limit, crash),
`fail_stale_jobs` fails the job and moves the video from `processing` to
`failed` ("Processing was interrupted. Try again later."), but only after the
stale timeout (65 minutes). A video whose queued message was lost (for
example after Redis data loss) stays `uploaded`.

## Running the server

```bash
uvicorn app.main:app --reload
```

The API is served at `http://127.0.0.1:8000`. Interactive docs are available at `http://127.0.0.1:8000/docs`.

## CORS configuration

Allowed cross-origin requests are configured with the `CORS_ORIGINS`
setting, a comma-separated list of origins:

```bash
# macOS / Linux
export CORS_ORIGINS="http://localhost:3000,https://app.example.com"

# Windows (PowerShell)
$env:CORS_ORIGINS = "http://localhost:3000,https://app.example.com"
```

CORS is configured with credentials enabled (`allow_credentials=True`), so
only the listed origins are allowed. The wildcard `*` is always ignored —
whether it comes from `CORS_ORIGINS` or from a `Settings` object passed
directly to `create_app` — and when no origins are configured no cross-origin
requests are allowed.

Rejected CORS preflight requests are answered directly by `CORSMiddleware`
with a plain-text `400` response, not the standard error format described
below.

## API prefix

Versioned endpoints are mounted under `/api/v1` (see `app/api/v1/__init__.py`).
Add new endpoints to `api_router` in that package. `GET /health` stays at the
root and is not versioned.

## Endpoints

| Method | Path                    | Request body                         | Response                                                                 |
| ------ | ----------------------- | ------------------------------------ | ------------------------------------------------------------------------ |
| GET    | `/health`               | —                                    | `{"status": "ok"}`                                                       |
| GET    | `/api/v1/ping`          | —                                    | `{"status": "ok"}`                                                       |
| POST   | `/api/v1/auth/register` | `{email, password, full_name?}`      | `201` public user; `409` email taken; `422` invalid email or password    |
| POST   | `/api/v1/auth/login`    | `{email, password}`                  | `200` `{access_token, token_type: "bearer"}` + `access_token` cookie; `401` |
| POST   | `/api/v1/auth/logout`   | —                                    | `204`, clears the `access_token` cookie                                  |
| GET    | `/api/v1/users/me`      | —                                    | `200` public user; `401`                                                 |
| PATCH  | `/api/v1/users/me`      | `{full_name?}`                       | `200` public user; `401`; `422` unknown field or name over 255 chars     |
| POST   | `/api/v1/users/me/password` | `{current_password, new_password}` | `200` `{access_token, token_type}` + new cookie; `400` wrong current password; `401`; `422` |
| POST   | `/api/v1/auth/verify-email/request` | — (authenticated)        | `202` email sent; `200` already verified; `401`; `429`; `502` delivery failed; `503` email not configured |
| POST   | `/api/v1/auth/verify-email/confirm` | `{token}`                | `200` `{message}`; `400` `invalid_token`; `422`; `429`                   |
| POST   | `/api/v1/auth/password-reset/request` | `{email}`              | `202` `{message}` (always the same); `422`; `429`; `503` email not configured |
| POST   | `/api/v1/auth/password-reset/confirm` | `{token, new_password}` | `204`, clears the cookie; `400` `invalid_token`; `422`; `429`          |
| GET    | `/api/v1/videos/upload-limits` | —                             | `200` `{max_upload_size_bytes, allowed_extensions, allowed_content_types}`; `401` |
| POST   | `/api/v1/videos`        | multipart, one file part `file`      | `201` `VideoRead`; `400` malformed multipart; `401`; `413` too large; `415` type not allowed or mismatched; `422` missing or empty file; `500`/`503` storage or database failure |
| GET    | `/api/v1/videos`        | — (query `limit` 1-100, default 20; `offset`; `status`) | `200` `{items: [VideoRead], total}`, newest first; `401`; `422` invalid parameter |
| GET    | `/api/v1/videos/{id}`   | —                                    | `200` `VideoRead`; `401`; `404` unknown or another user's video; `422` invalid id |
| DELETE | `/api/v1/videos/{id}`   | —                                    | `204`; `401`; `403` untrusted origin (cookie auth); `404`; `500` database failure (nothing deleted); `503` storage misconfigured |
| GET    | `/api/v1/videos/{id}/media` | — (optional `Range: bytes=...`)  | `307` to a short-lived URL (S3), or `200` whole file / `206` byte range; `401`; `404` (also `media_not_found`); `416` invalid or unsatisfiable range; `503` storage failure |
| POST   | `/api/v1/videos/{id}/transcription` | `{language?}` (optional) | `202` `{job_id, status}`; `401`; `403` untrusted origin (cookie auth); `404`; `409` `video_not_ready` / `transcription_in_progress`; `422` invalid language or field; `503` job could not be queued |
| GET    | `/api/v1/videos/{id}/transcript` | —                           | `200` transcript with segments; `401`; `404` (also `transcript_not_found`) |
| GET    | `/api/v1/videos/{id}/transcript/search` | — (query `q` 1-200 chars, `limit` 1-500) | `200` `{query, total, items: [segment]}`; `401`; `404`; `422` |
| GET    | `/api/v1/videos/{id}/transcript.srt` | —                       | `200` SubRip download; `401`; `404` (also `transcript_not_found`)       |
| GET    | `/api/v1/videos/{id}/transcript.vtt` | —                       | `200` WebVTT download; `401`; `404` (also `transcript_not_found`)       |
| GET    | `/api/v1/jobs/{id}`     | —                                    | `200` `{id, job_type, status, video_id, attempts, max_attempts, error_message, created_at, started_at, finished_at}`; `401`; `404` unknown or another user's job; `422` invalid id |

The auth endpoints also return `403` (`csrf_failed`) for requests from an
untrusted origin (see Authentication below), and so do the `/users/me`
endpoints when a cookie-authenticated `PATCH`/`POST` comes from one.

### Profile endpoints

- All `/api/v1/users/me` endpoints require authentication
  (`get_current_user`) and only ever act on the authenticated user.
- `PATCH` changes only `full_name` (trimmed; `null` or blank clears it; an
  omitted field is left unchanged). Any other field — including `email`,
  `is_admin` and `is_active` — is rejected with `422`.
- The password change verifies `current_password` and applies the
  registration rules to `new_password` (8 to 256 characters). It increments
  the user's `token_version`, so every previously issued token (including the
  one used for the request) stops working; the response contains a fresh
  token and sets it as the cookie.
- Responses use the public user schema, so the password hash, `is_admin` and
  `token_version` are never returned; the email is the stored, normalized
  (trimmed, lowercased) address.

## Authentication

- **Register** validates the email with `email-validator` (ASCII addresses
  only), strips and lowercases it — the same normalization as the `users`
  table, so `Alice@Example.com` and `alice@example.com` are one account — and
  requires a password of 8 to 256 characters. The response is the public user
  (`id`, `email`, `full_name`, `is_active`, `email_verified`, `created_at`);
  the password hash is never returned.
- **Login** returns the same `401 invalid_credentials` response ("Incorrect
  email or password.") for an unknown email, a wrong password, and an
  inactive user, and hashes a dummy password for unknown emails so response
  times do not reveal which emails exist.
- **Access tokens** are HS256 JWTs (PyJWT) signed with `SECRET_KEY`, with
  `sub` (user id), `iat`, `exp` (`ACCESS_TOKEN_EXPIRE_MINUTES` after issue) and
  `ver` (the user's `token_version`) claims. Helpers live in
  `app/core/security.py` (`create_access_token`, `decode_access_token`).
- **Cookie**: login also sets `access_token` with `HttpOnly`, `SameSite=Lax`,
  `Path=/`, `Max-Age` equal to the token lifetime, and `Secure` when
  `APP_ENV=production`. Logout expires it with the same attributes.
- **Current user**: protected routes depend on `get_current_user` from
  `app/api/deps.py`. It reads `Authorization: Bearer <token>` or, if that
  header is absent, the cookie, and returns `401` for a missing, invalid or
  expired token, an unknown or inactive user, or a token whose `ver` no
  longer matches the user's `token_version`.
- **Admins**: admin-only routes depend on `get_current_admin`, which returns
  `401` like `get_current_user` and `403` for an authenticated non-admin.
- **Logout does not revoke tokens.** JWTs are stateless: logout only clears
  the browser cookie, and a token copied before logout stays valid until it
  expires. Incrementing `users.token_version` (done automatically by a
  password reset) invalidates every token issued to that user; there is no
  per-token revocation list.

### CSRF protection

Because the cookie is sent automatically by browsers, state-changing
requests (anything except `GET`, `HEAD`, `OPTIONS`, `TRACE`) are checked in
addition to `SameSite=Lax` (`app/core/csrf.py`):

- The `Origin` header (or, without it, the origin of `Referer`) must be the
  API's own origin or one listed in `CORS_ORIGINS`; otherwise — including
  `Origin: null` — the request gets `403 csrf_failed`. Put the frontend's
  origin in `CORS_ORIGINS`.
- A request with neither header is accepted only if it carries no
  `access_token` cookie (non-browser clients). Clients that authenticate with
  `Authorization: Bearer` are not subject to the check.
- The check runs on all `/api/v1/auth/*` endpoints and on every
  cookie-authenticated request through `get_current_user`.
- The API's own origin is taken from the request URL. Behind a reverse proxy
  that terminates TLS, the backend may see `http://` while browsers send
  `https://`; either let the server trust forwarded headers (for example
  `uvicorn --proxy-headers --forwarded-allow-ips=<proxy ip>`) or list the
  public origin in `CORS_ORIGINS`, or same-origin requests get `403`.

### Email verification and password reset

Endpoints (`app/api/v1/auth.py`, all under `/api/v1/auth` and subject to the
CSRF origin check):

- **Registration** creates an email verification token and emails it after
  the response is sent. If email is not configured or delivery fails, the
  account is still created (the failure is logged) and the user can ask
  again with `verify-email/request`. `email_verified` stays `false` until a
  verification token is redeemed.
- `verify-email/request` requires authentication and only acts on the
  current user. It sends synchronously, so a delivery failure returns `502`,
  never a success. Each new token invalidates the previous one.
- `verify-email/confirm` needs no login (the token proves ownership of the
  address) and sets `users.email_verified_at`.
- `password-reset/request` returns the same `202` body for known, unknown,
  malformed and inactive addresses. The token is created in the request but
  the email is delivered by a background task after the response, so
  delivery errors do not change the response; they are logged as
  `email_delivery_failed`.
- `password-reset/confirm` sets the new password (8 to 256 characters;
  a rejected password does not use up the token), increments
  `token_version` so every existing access token stops working, invalidates
  all outstanding reset tokens, and clears the caller's auth cookie.
- Every invalid token — malformed, unknown, expired, already used, superseded,
  or of the other purpose — gets the same `400 invalid_token` response.
- Rate limits (`AccountRateLimiters` in `app/api/deps.py`, one set per app):
  5 verification or reset emails per account/address per hour, and 10 token
  redemptions and 10 reset requests per client address per 15 minutes.
  Exceeding them returns `429` with `Retry-After`. Reset requests are limited
  per address whether or not it has an account.
- Raw tokens are only ever in the email (in the link's URL fragment). They
  are never returned by the API or logged; logs carry only user ids, event
  names and email template names.

Email delivery (`app/services/email.py`) is selected by `EMAIL_PROVIDER`.
`fake` (the default) records messages in memory and needs no credentials;
it is used by tests and local development and is refused in production.
`smtp` uses the standard library `smtplib` with `SMTP_TIMEOUT_SECONDS` and
STARTTLS or implicit TLS. A missing or invalid email configuration never
stops the app from starting; the email endpoints return `503
email_unavailable` and the logs name the missing settings. See
`docs/setup/email.md` for production configuration.

The building blocks:

- `app/models/auth_token.py` — the `auth_tokens` table. Tokens are 256-bit
  random values (`secrets.token_urlsafe`); only their SHA-256 hash is stored.
  Each token has a purpose (`email_verification` or `password_reset`), an
  expiry, and `used_at`.
- `app/repositories/auth_tokens.py` — `create_token` (invalidates earlier
  tokens of the same purpose), `consume_token` (single use, enforced by a
  conditional `UPDATE`, so concurrent replays fail), `invalidate_tokens`.
  Malformed, unknown, expired, used and wrong-purpose tokens all raise the
  same `InvalidOneTimeTokenError`.
- `app/services/account_tokens.py` — `send_email_verification`,
  `verify_email` (sets `users.email_verified_at`), `request_password_reset`
  (silent for unknown, malformed or inactive addresses, so responses never
  reveal whether an account exists) and `reset_password` (sets the new
  password and increments `token_version`, invalidating existing sessions).
  Requests and attempts are rate-limited with the limiter passed in.
- `app/core/rate_limit.py` — `InMemoryRateLimiter` (sliding window).
- `app/services/email.py` — the `EmailSender` interface,
  `FakeEmailSender` (records messages in memory for tests and local
  development, no credentials), `SmtpEmailSender`, the plain-text templates,
  and `build_email_sender(settings)`.

### Limitations

- No per-token revocation: logout does not invalidate an issued JWT (see
  above). Keep `ACCESS_TOKEN_EXPIRE_MINUTES` short.
- There is no login or password-change rate limiting or account lockout
  yet, and the in-memory
  rate limiter is per process and resets on restart; a shared limiter is part
  of the security hardening work (CF-042). With several workers or replicas
  each process has its own limits, so the effective limits are multiplied.
- Per-client limits use `request.client.host`. Behind a reverse proxy, run
  the server with `--proxy-headers --forwarded-allow-ips=<proxy ip>`;
  otherwise every client shares the proxy's address and one rate-limit
  bucket.
- Registration returns `409` for a taken email, as required, which does
  reveal that the address is registered.
- Password reset requests deliver email after the response, but the token
  row is still written synchronously for known addresses, so response times
  can differ slightly between known and unknown addresses.
- Email is delivered in-process (FastAPI background tasks) without retries;
  a message lost to a delivery error or a restart must be requested again.
  A job queue with retries is future work.
- Emails are plain text only. The frontend pages `/verify-email` and
  `/reset-password` that read the token from the URL fragment are not part of
  the backend.

## Error format

Errors handled by the application's exception handlers (`AppError`,
`HTTPException`, and `RequestValidationError`) use the same JSON shape:

```json
{
  "error": {
    "code": "not_found",
    "message": "Not Found",
    "details": null
  }
}
```

- `code` — machine-readable error code.
- `message` — human-readable description.
- `details` — extra structured information, or `null`.

The handlers live in `app/core/errors.py`:

- `AppError(status_code, code, message, details=None)` — raise this from
  application code to return a specific status and code.
- `HTTPException` (including unknown routes and unsupported methods) — the
  original status code and headers (for example `Allow` or
  `WWW-Authenticate`) are kept, except headers that describe the original
  body (`Content-Length`, `Content-Type`, `Content-Encoding`, `ETag`, etc.),
  which are dropped and recomputed for the JSON body. Headers that describe
  the target resource rather than the error body are kept for specific
  statuses: `Content-Range` on `416` (the resource's size) and `ETag` on `412`
  (the current validator, so clients can retry a conditional update). `code`
  is derived from
  the status, e.g. `not_found` or `method_not_allowed`. For 5xx statuses the
  message is always the generic status phrase so internal details are not
  exposed. Statuses that must not carry a body (`204`, `205`, `304`) are
  returned with an empty body and their original headers (minus framing
  headers such as `Content-Length`) instead of the JSON shape.
- `RequestValidationError` — returns `422` with code `validation_error`.
  `details` is a list of `{"loc": [...], "msg": str, "type": str}` objects.
  `msg` is a fixed public message chosen by error `type`; raw validator
  messages, submitted input, and validation context are never returned,
  because they can contain request data or internal exception text. Only
  error types from the public allowlist in `app/core/errors.py` are returned;
  any other type (including custom types) is reported as `value_error` with
  the message `"Invalid value."`. In `loc`, the location source (`body`,
  `query`, ...), list indices, and parameter or field names declared by the
  route are kept; any other segment (for example a submitted dictionary key or
  a forbidden extra field name) is replaced with `"*"`.

## Running the tests

`pytest` is included in `requirements.txt`, so the backend tests need no extra
packages:

```bash
pytest
```

`tests/test_ci_workflow.py` also checks the CI workflow
(`.github/workflows/ci.yml`): that it is valid YAML with read-only permissions,
no secrets or deployment steps, SHA-pinned actions, and the required jobs. It
needs only the runtime requirements; when `PyYAML` from `requirements-dev.txt`
is installed, it also compares the result with a full YAML parser.

The backend tests use isolated SQLite databases (in memory, or a temporary file
for the migration tests in `tests/test_migrations.py`) and do not require
PostgreSQL or `DATABASE_URL` to be set. Background job tests run Celery
eagerly and mock broker publishing, so they need no Redis. The S3 storage tests use
`botocore.stub.Stubber` and mocked clients, so they need no network access or
S3 credentials. The FFmpeg tests generate a 2-second test video at runtime
with `ffmpeg -f lavfi` (no media files are committed); tests that need the
real `ffmpeg`/`ffprobe` binaries are skipped, with the reason shown by
`pytest -rs`, when they are not installed. The transcription provider tests
use `httpx.MockTransport` and fakes, so they need no network, API key or
faster-whisper. They ignore `backend/.env` and clear the
`get_settings()` cache between tests (see `tests/conftest.py`).

## Linting, formatting, and dependency audit

Development tools (`ruff`, `pip-audit`, `cyclonedx-bom`, and `PyYAML` for the
CI workflow tests) are listed in `requirements-dev.txt`, separately from the
runtime requirements:

```bash
pip install -r requirements-dev.txt

ruff check .
ruff format --check .   # or `ruff format .` to apply formatting
pip-audit -r requirements.txt
```

`requirements.txt` pins `dnspython>=2.9.2` (a transitive dependency of
`email-validator`) to pick up the fixes for advisories reported by
`pip-audit`. dnspython 2.9.x requires Python 3.11 or newer, which is why the
backend no longer supports Python 3.10.

## Continuous integration

The `backend` job in `.github/workflows/ci.yml` runs these checks and
`pytest` on Python 3.11 and 3.12 for every pull request and push to `main`,
with FFmpeg (built with libass) installed. See the root `README.md` for all
CI jobs.
