"""Checks for reproducible dependencies, Dependabot, coverage and type checks.

- Every backend requirement is pinned to an exact version (`==`).
- `.github/dependabot.yml` updates pip, npm and GitHub Actions weekly.
- Coverage of `backend/app` and gradual mypy checking are configured in
  `backend/pyproject.toml`, and the backend code passes mypy.
"""

import re
import tomllib

import pytest

from tests.test_ci_workflow import BACKEND_DIR, REPO_ROOT, _WorkflowParser

REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
PYPROJECT_PATH = BACKEND_DIR / "pyproject.toml"
TESTING_STRATEGY_PATH = (
    REPO_ROOT / "docs" / "engineering-quality" / "testing-strategy.md"
)

# name[extras]==version, nothing else (no ranges, URLs or markers).
PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?==[0-9][A-Za-z0-9.+!-]*$"
)

EXPECTED_DEPENDABOT_UPDATES = {
    ("pip", "/backend"),
    ("npm", "/frontend"),
    ("github-actions", "/"),
}


def _requirement_lines(filename: str) -> list[str]:
    text = (BACKEND_DIR / filename).read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _pyproject() -> dict:
    with PYPROJECT_PATH.open("rb") as file:
        return tomllib.load(file)


@pytest.mark.parametrize("filename", REQUIREMENT_FILES)
def test_every_requirement_is_pinned_exactly(filename):
    requirements = [
        line for line in _requirement_lines(filename) if line != "-r requirements.txt"
    ]

    assert requirements
    for line in requirements:
        assert PINNED_REQUIREMENT.match(line), line


def test_pinned_requirement_pattern_rejects_ranges():
    for line in (
        "fastapi>=0.110",
        "SQLAlchemy>=2.0,<3.0",
        "ruff~=0.12",
        "boto3",
        "celery[redis]>=5.3",
        "pytest==8.*,<9",
        "httpx==0.28.1; python_version<'3.12'",
    ):
        assert not PINNED_REQUIREMENT.match(line), line
    for line in ("fastapi==0.121.0", "celery[redis]==5.5.3", "PyYAML==6.0.3"):
        assert PINNED_REQUIREMENT.match(line), line


def test_dev_requirements_include_the_runtime_requirements():
    assert _requirement_lines("requirements-dev.txt")[0] == "-r requirements.txt"


def test_requirements_have_no_duplicates():
    names = [
        re.split(r"[\[=]", line, maxsplit=1)[0].lower().replace("_", "-")
        for filename in REQUIREMENT_FILES
        for line in _requirement_lines(filename)
        if not line.startswith("-r ")
    ]

    assert len(names) == len(set(names))


def test_dependabot_updates_pip_npm_and_actions_weekly():
    config = _WorkflowParser(DEPENDABOT_PATH.read_text(encoding="utf-8")).parse()

    assert config["version"] == 2
    updates = config["updates"]
    assert {
        (update["package-ecosystem"], update["directory"]) for update in updates
    } == EXPECTED_DEPENDABOT_UPDATES
    for update in updates:
        assert update["schedule"] == {"interval": "weekly"}, update


def test_dependabot_directories_exist():
    assert (BACKEND_DIR / "requirements.txt").is_file()
    assert (REPO_ROOT / "frontend" / "package.json").is_file()
    assert (REPO_ROOT / ".github" / "workflows").is_dir()


def test_dependabot_matches_pyyaml():
    yaml = pytest.importorskip("yaml")
    text = DEPENDABOT_PATH.read_text(encoding="utf-8")

    assert yaml.safe_load(text) == _WorkflowParser(text).parse()


def test_coverage_measures_app_without_a_threshold():
    coverage = _pyproject()["tool"]["coverage"]

    assert coverage["run"]["source"] == ["app"]
    assert coverage["report"]["show_missing"] is True
    assert "fail_under" not in coverage["report"]
    assert "--cov-fail-under" not in (BACKEND_DIR / "pytest.ini").read_text(
        encoding="utf-8"
    )


def test_mypy_checks_app_gradually():
    mypy_config = _pyproject()["tool"]["mypy"]

    assert mypy_config["files"] == ["app"]
    assert "pydantic.mypy" in mypy_config["plugins"]
    # Gradual adoption: not strict yet, and no blanket error suppression.
    assert mypy_config.get("strict", False) is False
    assert "ignore_errors" not in mypy_config
    assert "disable_error_code" not in mypy_config


def test_backend_code_passes_mypy():
    api = pytest.importorskip("mypy.api")

    stdout, stderr, exit_status = api.run(
        [
            "--config-file",
            str(PYPROJECT_PATH),
            "--cache-dir",
            str(BACKEND_DIR / ".mypy_cache"),
            str(BACKEND_DIR / "app"),
        ]
    )

    assert exit_status == 0, stdout + stderr


def test_testing_strategy_documents_coverage_and_type_checks():
    text = TESTING_STRATEGY_PATH.read_text(encoding="utf-8")

    assert "--cov=app" in text
    assert "mypy" in text
    for module in ("auth", "upload", "jobs", "billing", "rendering"):
        assert module in text.lower(), module
