# ClipForge AI Backend

FastAPI backend for ClipForge AI.

## Requirements

- Python 3.10 or newer

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
| `REDIS_URL`                   | unset                    | Optional Redis connection string.                            |
| `SECRET_KEY`                  | development-only default | Secret for signing tokens. Required in production.           |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30`                     | Access token lifetime in minutes (must be positive).         |
| `CORS_ORIGINS`                | empty                    | Comma-separated list of allowed origins (see below).         |
| `MAX_UPLOAD_SIZE_MB`          | `500`                    | Maximum upload size in megabytes (must be positive).         |
| `STORAGE_BACKEND`             | `local`                  | `local` or `s3` (see Storage below).                         |
| `LOCAL_STORAGE_DIR`           | `backend/storage`        | Root directory of the local storage backend.                 |
| `S3_ENDPOINT_URL`             | unset                    | S3 API URL for R2/MinIO; leave unset for AWS S3.             |
| `S3_REGION`                   | unset                    | Region (`auto` for R2). Required for `s3`.                   |
| `S3_BUCKET`                   | unset                    | Bucket name. Required for `s3`.                              |
| `S3_ACCESS_KEY_ID`            | unset                    | Access key ID. Required for `s3`.                            |
| `S3_SECRET_ACCESS_KEY`        | unset                    | Secret access key. Required for `s3`.                        |
| `S3_ADDRESSING_STYLE`         | `auto`                   | `auto`, `path` (MinIO), or `virtual`.                        |

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

| Method | Path           | Response             |
| ------ | -------------- | -------------------- |
| GET    | `/health`      | `{"status": "ok"}`   |
| GET    | `/api/v1/ping` | `{"status": "ok"}`   |

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

```bash
pytest
```

The tests use isolated SQLite databases (in memory, or a temporary file for
the migration tests in `tests/test_migrations.py`) and do not require
PostgreSQL or `DATABASE_URL` to be set. The S3 storage tests use
`botocore.stub.Stubber` and mocked clients, so they need no network access or
S3 credentials. They ignore `backend/.env` and clear the
`get_settings()` cache between tests (see `tests/conftest.py`).
