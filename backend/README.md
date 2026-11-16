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
| `STORAGE_BACKEND`             | `local`                  | `local` or `s3`.                                             |

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

The tests use an in-memory SQLite database and do not require PostgreSQL or
`DATABASE_URL` to be set. They ignore `backend/.env` and clear the
`get_settings()` cache between tests (see `tests/conftest.py`).
