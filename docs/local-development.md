# Local development

## Backing services (Docker Compose)

> **Local development only.** `docker-compose.yml`, the root `.env.example`,
> and `backend/.env.example` contain development-only credentials. They are
> public in this repository and are not secret. **Never use them in
> production** or on any machine reachable from a network you do not control.

The root `docker-compose.yml` runs the services the backend depends on. The
application itself is not containerized; run the backend as described in
`backend/README.md`.

| Service    | Image                                      | Host port(s)                      | Data volume     |
| ---------- | ------------------------------------------ | --------------------------------- | --------------- |
| `postgres` | `postgres:16`                              | `5433`                            | `postgres-data` |
| `redis`    | `redis:7.4`                                | `6379`                            | none            |
| `minio`    | `minio/minio:RELEASE.2025-04-22T22-12-26Z` | `9000` (S3 API), `9001` (console) | `minio-data`    |

Ports are bound to `127.0.0.1`, so the services are only reachable from your
machine. PostgreSQL is published on host port `5433` instead of the default
`5432` so it does not clash with a PostgreSQL server already installed on your
machine; inside the container it still listens on `5432`.

### Requirements

- Docker with the Compose plugin (`docker compose`).

### Configure credentials

The project ships one set of development-only credentials, used consistently
in three places:

| File                   | Purpose                                                                                     |
| ---------------------- | ------------------------------------------------------------------------------------------- |
| `docker-compose.yml`   | Fallback defaults for `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`. |
| `.env.example` (root)  | Template for the Compose variables, with the same values as the defaults.                  |
| `backend/.env.example` | Template for the backend variables, pointing at the Compose services with the same values. |

No setup is required to start the services: Compose uses the defaults in
`docker-compose.yml` whenever a variable is not supplied.

To customize the Compose credentials, copy the root template to `.env`, which
Docker Compose reads automatically (it does **not** read `.env.example`), and
edit the copy:

```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

Variables exported in your shell also override the defaults. `.env` is
ignored by Git. Even in `.env`, use only local development values — never
production credentials.

### Start and stop services

Run these from the repository root:

```bash
# Check the configuration
docker compose config

# Start all services in the background
docker compose up -d

# Show status and health
docker compose ps

# Follow logs
docker compose logs -f

# Stop services (data in volumes is kept)
docker compose down
```

### Reset volumes

To delete all local PostgreSQL and MinIO data and start fresh:

```bash
docker compose down -v
```

This removes the `clipforge-dev_postgres-data` and `clipforge-dev_minio-data`
volumes. The next `docker compose up -d` starts with empty services.

### Connecting the backend

The backend reads its settings from the process environment and does not load
`.env` files. Set the required variables in your local shell (or your IDE's
run configuration) before starting the server; see `backend/README.md` for
the exact commands. Do not commit files containing these values.

`backend/.env.example` lists ready-to-use backend values for the default
Compose credentials. Use it as the reference when setting the variables in
your shell. The backend settings correspond to the Compose services as
follows:

- `DATABASE_URL` (required) — the `postgres` service at `localhost:5433`,
  built from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`:
  `postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@localhost:5433/<POSTGRES_DB>`.
- `REDIS_URL` — the `redis` service at `redis://localhost:6379/0` (no
  authentication).
- `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION` —
  the `minio` S3 API at `http://localhost:9000`. The access key and secret key
  are `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`.
- `CORS_ORIGINS` (optional) — not related to Compose; for example
  `http://localhost:3000` for the local frontend.

The backend currently reads only `DATABASE_URL` and `CORS_ORIGINS`; the Redis
and S3 variables are provided for features that will use them.

If you change values in the root `.env`, update the backend variables to
match.

The MinIO web console is available at `http://localhost:9001`; sign in with
`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`.

Changing `POSTGRES_*` values after the `postgres-data` volume has been created
has no effect on the existing database; reset the volumes to apply them.
