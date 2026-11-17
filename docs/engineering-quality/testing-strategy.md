# Testing strategy: coverage and type checks

This page explains how to measure backend test coverage and run the type
checker, and which modules' coverage we track. Run every command from the
`backend/` directory after installing the development requirements:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

## Reproducible dependencies

Every line in `backend/requirements.txt` and `backend/requirements-dev.txt`
pins an exact version (`==`), so the same versions are installed on every
machine and in CI. `tests/test_dependency_policy.py` fails if a version range
is added. Most transitive dependencies (for example `pydantic`) are not
pinned. They resolve within the ranges that the pinned packages allow. A
transitive dependency is pinned directly only when an older release has a
known vulnerability, as with `starlette` and `dnspython`.

Dependabot (`.github/dependabot.yml`) checks for updates every week for:

| Ecosystem        | Directory   |
| ---------------- | ----------- |
| `pip`            | `/backend`  |
| `npm`            | `/frontend` |
| `github-actions` | `/`         |

Each update arrives as a pull request and must pass CI like any other change.
To update a pin by hand, change the version, reinstall, and run the checks
below.

## Coverage

Coverage is measured with `pytest-cov` (coverage.py). The configuration is in
`backend/pyproject.toml` (`[tool.coverage.*]`): it measures `app/` with branch
coverage and lists the missing lines.

```bash
python -m pytest --cov=app --cov-report=term-missing

# Optional HTML report in backend/htmlcov/ (git-ignored)
python -m pytest --cov=app --cov-report=html
```

A plain `python -m pytest` run does not collect coverage, and it works
without `pytest-cov` installed. The CI `backend` job runs the command above
and prints the report in the job log.

No failure threshold (`fail_under`) is set yet. The current numbers are a
baseline. Add a threshold once the critical modules below have the tests they
need.

## Critical modules

Track the coverage of these areas in the report. Every new behaviour in them
needs tests for the normal, boundary and failure cases.

| Area      | Modules                                                                                                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------------ |
| auth      | `app/api/v1/auth.py`, `app/api/deps.py`, `app/core/security.py`, `app/core/csrf.py`, `app/services/account_tokens.py`, `app/repositories/auth_tokens.py` |
| upload    | `app/api/v1/videos.py`, `app/services/video_upload.py`, `app/storage/`                                                    |
| jobs      | `app/worker/` (including `jobs.py` and `metadata.py`), `app/models/processing_job.py`                                    |
| billing   | Not implemented yet. Add its modules here when the billing work lands.                                                    |
| rendering | `app/media/` (FFmpeg wrappers and subtitle rendering) and the `render` job tasks as they are added                        |

## Type checking (mypy)

`mypy` checks `app/` with the configuration in `backend/pyproject.toml`
(`[tool.mypy]`):

```bash
mypy
```

The configuration supports gradual adoption:

- Only annotated functions are checked (mypy's default). Unannotated code is
  skipped until someone adds annotations.
- The `pydantic.mypy` plugin understands the `Settings` and schema models.
- `ignore_missing_imports` covers libraries without type information (Celery,
  boto3/botocore).
- No error codes are disabled globally. Fix new errors in the code. Use a
  targeted `# type: ignore[code]` with a reason only when a fix isn't
  practical.

To tighten the checks, enable stricter options (for example
`disallow_untyped_defs`) for one package at a time with a
`[[tool.mypy.overrides]]` section, starting with the critical modules.

The CI `backend` job runs `mypy`. The test suite runs it too
(`tests/test_dependency_policy.py::test_backend_code_passes_mypy`), so a type
error fails `pytest`. That test is skipped when mypy is not installed.

## Limitations

- Transitive dependencies are not locked. A new release of one can still
  change a build until a lock file (for example `pip-tools` hashes) is
  introduced.
- Coverage has no enforced minimum yet, and nothing tracks the coverage of
  each critical module automatically. Read it from the report.
- `tests/` and `migrations/` are not type-checked yet.
