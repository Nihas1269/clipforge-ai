# ClipForge AI

ClipForge AI is a SaaS application for automatically repurposing long-form videos into short-form content.

## Status

**Under development.** ClipForge AI is in an early stage. The features below are planned and are not yet available.

## Planned features

- **Video upload**: Upload and store long-form videos.
- **Transcription**: Automatically transcribe video audio and generate subtitles.
- **AI clip detection**: Use AI to find the most interesting moments for short-form clips.
- **Vertical reframing**: Reframe horizontal footage into vertical formats for short-form platforms.
- **Captions**: Add and customize captions on generated clips.
- **Export**: Preview finished clips and export them.

## Continuous integration

The CI workflow (`.github/workflows/ci.yml`) runs on every pull request and on
every push to `main`. It only validates code: it never deploys, publishes, or
merges, uses no repository secrets, and runs with a read-only `GITHUB_TOKEN`
(`contents: read`). Every third-party action is pinned to a full commit SHA.

| Job             | Checks                                                                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend`       | On Python 3.11 and 3.12: installs FFmpeg (with libass), `ruff check`, `ruff format --check`, `mypy`, `pytest` with a coverage report for `app/` (including the workflow policy tests), `pip-audit -r requirements.txt`. |
| `frontend`      | On Node.js 24 (LTS): `npm ci`, `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`, `npm audit --audit-level=high`.            |
| `security`      | `gitleaks` secret scan of the full Git history (configuration in `.gitleaks.toml`).                                                         |
| `workflow-lint` | `actionlint` and `zizmor` against every workflow in `.github/workflows/`.                                                                   |

`ci.yml` replaces the former `.github/workflows/frontend.yml`; its checks are
part of the `frontend` job.

Because the backend job uses a Python matrix, its checks appear as
`backend (3.11)` and `backend (3.12)`.

The `backend` (Python 3.12) and `frontend` jobs also generate CycloneDX SBOMs
of the runtime dependencies and upload them as the workflow artifacts
`sbom-backend` and `sbom-frontend`. They are not published anywhere else.

Dependabot (`.github/dependabot.yml`) opens weekly update pull requests for
the backend (pip), frontend (npm) and GitHub Actions dependencies.

To run the same checks locally, see `backend/README.md` and run the npm
scripts listed above in `frontend/`.

Limitations: the workflow as a whole can only be verified when it runs on
GitHub for a pull request. `gitleaks`, `actionlint`, and `zizmor` (including
its online check that each pinned SHA belongs to the named action) are not
part of the local setup and are first run there; the Python 3.11/3.12 matrix
is likewise only exercised in CI unless both interpreters are installed
locally.
