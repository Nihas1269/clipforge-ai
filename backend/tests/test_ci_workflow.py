"""Checks for the CI setup, including `.github/workflows/ci.yml`.

The workflow itself only runs on GitHub, so these tests check the guarantees
it must keep: valid YAML, read-only permissions, no secrets or deployment
steps, actions pinned to full commit SHAs, and the required checks.

They live in the backend suite so they run wherever `pytest` runs in
`backend/` (locally and in the CI `backend` job), and read the workflow and
other repository files relative to the repository root.

The workflow is parsed with a small strict parser for the YAML subset it uses
(block mappings and sequences, plain and quoted scalars, `|` literal blocks,
comments), so these tests need only the runtime requirements. Anything outside
that subset fails the test. When PyYAML from `requirements-dev.txt` is
installed (as in CI), the workflow is also loaded with PyYAML and must give the
same result.
"""

import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED_JOBS = ("backend", "frontend", "security", "workflow-lint")
MAX_TIMEOUT_MINUTES = 60

# owner/repo[/path]@<40 hex characters>
PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
# A `uses:` line followed by a trailing version comment, e.g. "# v4.2.2".
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d+(\.\d+)*\s*$")

FORBIDDEN_RUN_PATTERNS = (
    "deploy",
    "npm publish",
    "twine upload",
    "docker push",
    "gh pr merge",
    "gh release",
    "git push",
)

_KEY = re.compile(r"^([A-Za-z_][\w.-]*):(?: +(.*))?$")
_DOUBLE_QUOTED = re.compile(r'^"([^"\\]*)"(.*)$')
_SINGLE_QUOTED = re.compile(r"^'((?:[^']|'')*)'(.*)$")
_TRAILING_COMMENT = re.compile(r"^(?:\s+#.*)?$")
_PLAIN_FORBIDDEN_START = tuple("[]{}&*!|>%@`#,?:-\"'")


class _WorkflowParser:
    """Strict parser for the YAML subset used by the workflow.

    Raises ValueError for anything it does not support instead of guessing.
    """

    def __init__(self, text: str):
        if "\t" in text:
            raise ValueError("tabs are not allowed in YAML indentation")
        self.lines = text.splitlines()
        self.pos = 0

    def parse(self):
        peek = self._peek()
        if peek is None or peek[0] != 0:
            raise ValueError("document must start with a top-level mapping")
        result = self._block(0)
        if self._peek() is not None:
            raise ValueError(f"unexpected content: {self.lines[self.pos]!r}")
        return result

    def _peek(self):
        while self.pos < len(self.lines):
            stripped = self.lines[self.pos].strip()
            if stripped and not stripped.startswith("#"):
                line = self.lines[self.pos]
                return len(line) - len(line.lstrip(" ")), stripped
            self.pos += 1
        return None

    def _block(self, indent: int):
        _, content = self._peek()
        if content == "-" or content.startswith("- "):
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict:
        result = {}
        while (peek := self._peek()) is not None and peek[0] >= indent:
            line_indent, content = peek
            match = _KEY.match(content)
            if line_indent != indent or match is None:
                raise ValueError(f"invalid mapping line: {self.lines[self.pos]!r}")
            key, raw = match.group(1), match.group(2)
            if key in result:
                raise ValueError(f"duplicate key: {key!r}")
            self.pos += 1
            if raw is None or raw.startswith("#"):
                child = self._peek()
                if child is not None and child[0] > indent:
                    result[key] = self._block(child[0])
                else:
                    result[key] = None
            elif raw == "|":
                result[key] = self._literal(indent)
            else:
                result[key] = _scalar(raw)
        return result

    def _sequence(self, indent: int) -> list:
        items = []
        while (peek := self._peek()) is not None and peek[0] >= indent:
            line_indent, content = peek
            is_item = content == "-" or content.startswith("- ")
            if line_indent != indent or not is_item:
                raise ValueError(f"invalid sequence line: {self.lines[self.pos]!r}")
            rest = content[1:].strip()
            if not rest:
                self.pos += 1
                child = self._peek()
                if child is None or child[0] <= indent:
                    raise ValueError("empty sequence item")
                items.append(self._block(child[0]))
            elif _KEY.match(rest):
                # "- key: value" starts a mapping indented past the dash.
                self.lines[self.pos] = " " * (indent + 2) + rest
                items.append(self._mapping(indent + 2))
            else:
                self.pos += 1
                items.append(_scalar(rest))
        return items

    def _literal(self, parent_indent: int) -> str:
        lines = []
        block_indent = None
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if not line.strip():
                lines.append("")
                self.pos += 1
                continue
            line_indent = len(line) - len(line.lstrip(" "))
            if line_indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = line_indent
            elif line_indent < block_indent:
                raise ValueError(f"bad literal indentation: {line!r}")
            lines.append(line[block_indent:])
            self.pos += 1
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            raise ValueError("empty literal block")
        return "\n".join(lines) + "\n"


def _scalar(raw: str):
    if raw.startswith(("'", '"')):
        pattern = _SINGLE_QUOTED if raw.startswith("'") else _DOUBLE_QUOTED
        match = pattern.match(raw)
        if match is None or not _TRAILING_COMMENT.match(match.group(2)):
            raise ValueError(f"unsupported quoted scalar: {raw!r}")
        if raw.startswith("'"):
            return match.group(1).replace("''", "'")
        return match.group(1)

    value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if not value or value.startswith(_PLAIN_FORBIDDEN_START) or ": " in value:
        raise ValueError(f"unsupported plain scalar: {raw!r}")
    if value.lower() in ("null", "~", "yes", "no", "on", "off", "y", "n"):
        raise ValueError(f"ambiguous plain scalar, quote it: {raw!r}")
    if value.lower() in ("true", "false") and value not in ("true", "false"):
        raise ValueError(f"ambiguous plain scalar, use lowercase: {raw!r}")
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    if re.fullmatch(r"[-+.0-9eE]+", value):
        raise ValueError(f"ambiguous numeric scalar, quote it: {raw!r}")
    return value


def _normalize_pyyaml(value):
    # YAML 1.1 (PyYAML) reads the bare key `on` as the boolean True; GitHub
    # Actions reads it as the string "on".
    if isinstance(value, dict):
        return {
            ("on" if key is True else key): _normalize_pyyaml(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_pyyaml(item) for item in value]
    return value


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    data = _WorkflowParser(workflow_text).parse()
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict) -> dict:
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    return triggers


def _steps(workflow: dict, job_id: str) -> list[dict]:
    return workflow["jobs"][job_id]["steps"]


def _run_commands(workflow: dict, job_id: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(workflow, job_id))


def _all_steps(workflow: dict) -> list[dict]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def test_parser_rejects_yaml_outside_the_supported_subset():
    for text in (
        "jobs:\n\tbackend: {}\n",
        "name: CI\nname: again\n",
        "on: {pull_request: null}\n",
        "jobs:\n  a: 1\n b: 2\n",
        "value: yes\n",
        "value: 3.10\n",
        'value: "unterminated\n',
        "run: |\nnext: 1\n",
    ):
        with pytest.raises(ValueError):
            _WorkflowParser(text).parse()


def test_parser_reads_the_supported_subset():
    text = (
        "# comment\n"
        "on:\n"
        "  push:\n"
        "    branches:\n"
        "      - main\n"
        "steps:\n"
        "  - name: A  # trailing comment\n"
        "    uses: org/action@abc # v1\n"
        "    with:\n"
        '      version: "3.10"\n'
        "      flag: false\n"
        "      depth: 0\n"
        "  - name: B\n"
        "    run: |\n"
        "      echo one\n"
        "\n"
        "      # still part of the script\n"
        "      echo 'two'\n"
        "    if: matrix.v == '3.12'\n"
        "    shell: '\"x\" -y'\n"
    )

    assert _WorkflowParser(text).parse() == {
        "on": {"push": {"branches": ["main"]}},
        "steps": [
            {
                "name": "A",
                "uses": "org/action@abc",
                "with": {"version": "3.10", "flag": False, "depth": 0},
            },
            {
                "name": "B",
                "run": "echo one\n\n# still part of the script\necho 'two'\n",
                "if": "matrix.v == '3.12'",
                "shell": '"x" -y',
            },
        ],
    }


def test_workflow_is_valid_yaml_with_a_name(workflow):
    assert workflow["name"] == "CI"


def test_workflow_matches_pyyaml(workflow, workflow_text):
    yaml = pytest.importorskip("yaml")

    assert _normalize_pyyaml(yaml.safe_load(workflow_text)) == workflow


def test_triggers_on_pull_request_and_push_to_main_only(workflow):
    triggers = _triggers(workflow)

    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"] == {"branches": ["main"]}


def test_never_uses_privileged_triggers(workflow_text):
    assert "pull_request_target" not in workflow_text
    assert "workflow_run" not in workflow_text


def test_workflow_permissions_are_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_no_job_requests_its_own_permissions(workflow):
    for job_id, job in workflow["jobs"].items():
        assert "permissions" not in job, job_id


def test_no_repository_secrets_are_used(workflow_text):
    assert "secrets." not in workflow_text
    assert "secrets[" not in workflow_text


def test_only_pull_request_runs_are_cancelled_by_newer_runs(workflow):
    concurrency = workflow["concurrency"]
    expected = "${{ github.event_name == 'pull_request' }}"

    assert concurrency["cancel-in-progress"] == expected
    # Pushes get a group per run, so a newer push cannot replace a queued one.
    assert "github.run_id" in concurrency["group"]


def test_required_jobs_have_stable_names(workflow):
    for job_id in REQUIRED_JOBS:
        assert workflow["jobs"][job_id]["name"] == job_id


def test_every_job_has_a_timeout(workflow):
    for job_id, job in workflow["jobs"].items():
        timeout = job.get("timeout-minutes")
        assert isinstance(timeout, int), job_id
        assert 0 < timeout <= MAX_TIMEOUT_MINUTES, job_id


def test_every_step_has_a_name(workflow):
    for step in _all_steps(workflow):
        assert step.get("name"), step


def test_every_action_is_pinned_to_a_full_commit_sha(workflow):
    uses = [step["uses"] for step in _all_steps(workflow) if "uses" in step]

    assert uses
    for ref in uses:
        assert PINNED_USES.match(ref), ref


def test_every_pinned_action_has_a_version_comment(workflow_text):
    uses_lines = [
        match
        for match in map(USES_LINE.match, workflow_text.splitlines())
        if match is not None
    ]

    assert uses_lines
    for match in uses_lines:
        assert VERSION_COMMENT.match(match.group(2)), match.group(0)


def test_checkouts_do_not_persist_credentials(workflow):
    checkouts = [
        step
        for step in _all_steps(workflow)
        if step.get("uses", "").startswith("actions/checkout@")
    ]

    assert len(checkouts) == len(workflow["jobs"])
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


def test_no_step_deploys_publishes_or_merges(workflow):
    for step in _all_steps(workflow):
        command = step.get("run", "").lower()
        for pattern in FORBIDDEN_RUN_PATTERNS:
            assert pattern not in command, (step["name"], pattern)


def test_backend_job_runs_on_python_311_and_312(workflow):
    matrix = workflow["jobs"]["backend"]["strategy"]["matrix"]

    assert matrix["python-version"] == ["3.11", "3.12"]


def test_backend_job_runs_checks(workflow):
    job = workflow["jobs"]["backend"]
    commands = _run_commands(workflow, "backend")

    assert job["defaults"]["run"]["working-directory"] == "backend"
    assert "apt-get install --yes --no-install-recommends ffmpeg" in commands
    assert "--enable-libass" in commands
    assert "pip install -r requirements.txt -r requirements-dev.txt" in commands
    assert "ruff check ." in commands
    assert "ruff format --check ." in commands
    assert "python -m pytest --cov=app --cov-report=term-missing" in commands
    assert re.search(r"^mypy$", commands, re.MULTILINE)
    assert "pip-audit -r requirements.txt" in commands


def test_frontend_job_runs_checks(workflow):
    job = workflow["jobs"]["frontend"]
    commands = _run_commands(workflow, "frontend")
    setup_node = next(
        step
        for step in _steps(workflow, "frontend")
        if step.get("uses", "").startswith("actions/setup-node@")
    )

    assert job["defaults"]["run"]["working-directory"] == "frontend"
    assert setup_node["with"]["cache"] == "npm"
    assert setup_node["with"]["cache-dependency-path"] == "frontend/package-lock.json"
    for command in (
        "npm ci",
        "npm run lint",
        "npm run typecheck",
        "npm test",
        "npm run build",
        "npm audit --audit-level=high",
    ):
        assert command in commands
    assert (REPO_ROOT / "frontend" / "package-lock.json").is_file()


def test_security_job_scans_full_history_with_gitleaks(workflow):
    checkout = _steps(workflow, "security")[0]
    commands = _run_commands(workflow, "security")

    assert checkout["with"]["fetch-depth"] == 0
    assert "gitleaks" in commands
    assert "--redact" in commands
    assert (REPO_ROOT / ".gitleaks.toml").is_file()


def test_workflow_lint_job_runs_actionlint_and_zizmor(workflow):
    commands = _run_commands(workflow, "workflow-lint")

    # Both linters cover every workflow in .github/workflows/, not only ci.yml.
    assert re.search(r'/bin/actionlint" -color$', commands, re.MULTILINE)
    assert re.search(r"^zizmor \.github/workflows/$", commands, re.MULTILINE)


def test_ci_replaces_the_frontend_workflow_and_keeps_one_copy_of_these_tests():
    # ci.yml's frontend job runs the checks of the former frontend.yml.
    assert not (REPO_ROOT / ".github" / "workflows" / "frontend.yml").exists()
    assert not (REPO_ROOT / ".github" / "tests" / "test_ci_workflow.py").exists()


@pytest.mark.parametrize(
    ("job_id", "artifact"),
    [("backend", "sbom-backend"), ("frontend", "sbom-frontend")],
)
def test_sbom_is_generated_and_uploaded_as_artifact(workflow, job_id, artifact):
    commands = _run_commands(workflow, job_id)
    uploads = [
        step
        for step in _steps(workflow, job_id)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]

    assert "cyclonedx" in commands
    assert [step["with"]["name"] for step in uploads] == [artifact]


def test_dev_tools_are_not_runtime_requirements():
    runtime = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").lower()
    dev = (BACKEND_DIR / "requirements-dev.txt").read_text(encoding="utf-8").lower()

    for tool in ("ruff", "pip-audit", "cyclonedx-bom", "pyyaml", "pytest-cov", "mypy"):
        assert tool in dev
        assert tool not in runtime
    assert "-r requirements.txt" in dev


def test_readmes_document_ci():
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    backend_readme = (BACKEND_DIR / "README.md").read_text(encoding="utf-8")

    assert ".github/workflows/ci.yml" in root_readme
    assert ".github/workflows/ci.yml" in backend_readme
    assert "requirements-dev.txt" in backend_readme
