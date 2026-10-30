"""docs/coverage.md, held to the suite it describes.

An `auto` row says a check runs in CI. If the check is renamed or removed, or
the job that ran it goes away, the row keeps claiming coverage nobody has. Each
test here makes one way of drifting fail, the way test_known_failures.py holds
its registry to the suite.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "coverage.md"
WORKFLOW = ROOT / ".github" / "workflows" / "mcp-evals.yml"
HEADER = ("test case", "axes", "how", "why not")
HOW = ("auto", "manual", "none")


@dataclass(frozen=True)
class Row:
    case: str
    axes: str
    how: str
    why_not: str

    @property
    def kind(self) -> str:
        return self.how.split(":", 1)[0].strip()

    @property
    def labels(self) -> list[str]:
        return re.findall(r'"([^"]+)"', self.how)

    @property
    def jobs(self) -> list[str]:
        return re.findall(r"`([^`]+)`", self.how)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_rows(text: str) -> list[Row]:
    """Every row of every table whose header is the coverage header."""
    rows: list[Row] = []
    in_table = False
    for line in text.splitlines():
        if not line.startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if tuple(cells) == HEADER:
            in_table = True
        elif in_table and not set("".join(cells)) <= set("-: "):
            assert len(cells) == len(HEADER), f"a coverage row needs {len(HEADER)} cells: {line}"
            rows.append(Row(*cells))
    return rows


def literals_by_module(directory: Path) -> dict[str, set[str]]:
    """Every string constant in each module, docstrings left out, by module name.

    f-strings count by their literal parts, which is all a runtime-built label
    has in the source. A docstring is prose about a check, not a check.
    """
    found: dict[str, set[str]] = {}
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        found[path.stem] = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        }
    return found


def label_literals(directory: Path) -> set[str]:
    found: set[str] = set()
    for literals in literals_by_module(directory).values():
        found |= literals
    return found


def workflow_jobs(text: str) -> set[str]:
    return set(_job_blocks(text))


def _job_blocks(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):$", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


@dataclass(frozen=True)
class Invocation:
    """One `python -m functional.<module>` a job runs."""

    module: str
    endpoint: str | None
    provisions: bool


def job_invocations(text: str) -> dict[str, list[Invocation]]:
    """What each workflow job runs of the functional suite.

    A continued shell line is joined first, so a flag on the next line still
    belongs to its command. The runner's endpoint defaults to admin, as its
    `--endpoint` does.
    """
    out: dict[str, list[Invocation]] = {}
    for job, block in _job_blocks(text).items():
        joined = re.sub(r"\\\n\s*", " ", block)
        calls: list[Invocation] = []
        for command in re.finditer(r"python -m functional\.(\w+)([^\n]*)", joined):
            module, rest = command.group(1), command.group(2)
            match = re.search(r"--endpoint (\w+)", rest)
            endpoint = match.group(1) if match else ("admin" if module == "runner" else None)
            calls.append(Invocation(module, endpoint, "--provision-principals" in rest))
        out[job] = calls
    return out


# A module run on its own entry point rather than through the runner.
STANDALONE = {"sessions", "resp"}
# A principal the suite has to create: only --provision-principals does.
PROVISIONED = ("integration", "non-admin", "OAuth user")


def _axis(axes: str, name: str) -> str:
    match = re.search(rf"{name}: ([^.]*)\.", axes)
    return match.group(1) if match else ""


def unmet(row: Row, invocations: dict[str, list[Invocation]], modules: dict[str, set[str]]) -> list[str]:
    """Why a job the row names would not run the checks the row names.

    A label is attributed to the modules whose literals contain it. A label from
    a standalone module needs that module run; any other needs the runner, on an
    endpoint the row lists (admin when it lists none), and with principals
    provisioned when the row's principal is one the suite has to create.
    """
    endpoints = {e.strip().lower() for e in _axis(row.axes, "endpoint").split(",") if e.strip()} or {"admin"}
    provision = any(word in _axis(row.axes, "principal") for word in PROVISIONED)
    problems: list[str] = []
    for job in row.jobs:
        calls = invocations.get(job, [])
        for label in row.labels:
            owners = {name for name, literals in modules.items() if any(label in literal for literal in literals)}

            def runs(owner: str, calls: list[Invocation] = calls) -> bool:
                if owner in STANDALONE:
                    return any(call.module == owner for call in calls)
                return any(
                    call.module == "runner" and call.endpoint in endpoints and (call.provisions or not provision)
                    for call in calls
                )

            if not any(runs(owner) for owner in owners):
                need = f"functional.runner on {'/'.join(sorted(endpoints))}"
                if provision:
                    need += " with --provision-principals"
                problems.append(f"`{job}` does not run {label!r} (needs {need}, or the module holding it)")
    return problems


ROWS = parse_rows(DOC.read_text())
AUTO = [row for row in ROWS if row.kind == "auto"]


# ---------------------------------------------------------------------------
# The parser, so a broken one cannot pass everything by finding nothing
# ---------------------------------------------------------------------------
def test_the_parser_reads_only_coverage_tables() -> None:
    text = "\n".join(
        [
            "| axis | values |",
            "|---|---|",
            "| revision | 2024-11-05 |",
            "",
            "| test case | axes | how | why not |",
            "|---|---|---|---|",
            '| proves a thing | revision | auto: `static`. "a label", "another" | |',
            "| a gap | principal | none | nobody wrote it |",
        ]
    )
    rows = parse_rows(text)
    assert [row.kind for row in rows] == ["auto", "none"]
    assert rows[0].jobs == ["static"] and rows[0].labels == ["a label", "another"]
    assert rows[1].why_not == "nobody wrote it"


def test_a_row_with_a_missing_cell_is_an_error() -> None:
    with pytest.raises(AssertionError, match="needs 4 cells"):
        parse_rows("| test case | axes | how | why not |\n|---|---|---|---|\n| one | two | auto |")


def test_literals_skip_docstrings_and_keep_fstring_parts(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        '"""a docstring label"""\n'
        "def f(rep, who):\n"
        '    """another docstring label"""\n'
        '    rep.check_pass(f"{who}: is listed no toolsets")\n'
    )
    literals = label_literals(tmp_path)
    assert ": is listed no toolsets" in literals
    assert not {"a docstring label", "another docstring label"} & literals


SAMPLE_WORKFLOW = """name: x
jobs:
  full:
    steps:
      - run: python -m functional.runner --endpoint admin \\
          --protocol-version 2025-11-25 --provision-principals
      - run: python -m functional.runner --endpoint store --advisory
  bare:
    steps:
      - run: python -m functional.runner
      - run: python -m functional.sessions --redis-url redis://x
"""
SAMPLE_MODULES = {"runner": {"a runner label"}, "principals": {"an integration label"}, "sessions": {"a redis label"}}


def test_job_invocations_join_continued_lines_and_default_the_endpoint() -> None:
    calls = job_invocations(SAMPLE_WORKFLOW)
    assert calls["full"] == [Invocation("runner", "admin", True), Invocation("runner", "store", False)]
    assert calls["bare"] == [Invocation("runner", "admin", False), Invocation("sessions", None, False)]


def _row(axes: str, how: str) -> Row:
    return Row("a case", axes, how, "")


def test_a_job_has_to_run_the_endpoint_the_row_names() -> None:
    calls = job_invocations(SAMPLE_WORKFLOW)
    row = _row("endpoint: Store.", 'auto: `full`, `bare`. "a runner label"')
    assert unmet(row, calls, SAMPLE_MODULES) == [
        "`bare` does not run 'a runner label' (needs functional.runner on store, or the module holding it)"
    ]


def test_a_created_principal_needs_the_job_to_provision_it() -> None:
    calls = job_invocations(SAMPLE_WORKFLOW)
    row = _row("principal: integration, flagged admin.", 'auto: `full`, `bare`. "an integration label"')
    assert unmet(row, calls, SAMPLE_MODULES) == [
        "`bare` does not run 'an integration label' "
        "(needs functional.runner on admin with --provision-principals, or the module holding it)"
    ]


def test_a_standalone_module_needs_its_own_command() -> None:
    calls = job_invocations(SAMPLE_WORKFLOW)
    row = _row("endpoint: admin, Store. store: Redis.", 'auto: `full`, `bare`. "a redis label"')
    assert [problem.split(" does")[0] for problem in unmet(row, calls, SAMPLE_MODULES)] == ["`full`"]


def test_a_label_no_module_holds_runs_nowhere() -> None:
    row = _row("endpoint: admin.", 'auto: `full`. "gone"')
    assert unmet(row, job_invocations(SAMPLE_WORKFLOW), SAMPLE_MODULES)


def test_the_document_has_rows_of_every_kind() -> None:
    assert len(AUTO) >= 20
    assert {row.kind for row in ROWS} == set(HOW)


# ---------------------------------------------------------------------------
# The document against the suite
# ---------------------------------------------------------------------------
def test_every_row_says_how_it_runs() -> None:
    for row in ROWS:
        assert row.kind in HOW, f"{row.case}: how must start with one of {HOW}, got {row.how!r}"


def test_every_auto_row_names_a_job_the_workflow_has() -> None:
    jobs = workflow_jobs(WORKFLOW.read_text())
    for row in AUTO:
        assert row.jobs, f"{row.case}: an auto row names the jobs that run it"
        assert set(row.jobs) <= jobs, f"{row.case}: no such job {sorted(set(row.jobs) - jobs)}"


def test_every_auto_row_names_a_label_the_suite_still_has() -> None:
    literals = label_literals(ROOT / "functional")
    for row in AUTO:
        assert row.labels, f"{row.case}: an auto row names at least one check label"
        for label in row.labels:
            assert any(label in literal for literal in literals), f"{row.case}: no check label contains {label!r}"


def test_every_job_a_row_names_runs_its_checks() -> None:
    """A job existing and a label existing are two facts. This ties them: the
    job runs the module the label is in, on the endpoint and with the principals
    the row claims, so a job that drops the runner or --provision-principals
    takes its rows with it."""
    calls = job_invocations(WORKFLOW.read_text())
    modules = literals_by_module(ROOT / "functional")
    problems = [f"{row.case}: {problem}" for row in AUTO for problem in unmet(row, calls, modules)]
    assert not problems, "\n".join(problems)


def test_every_gap_says_why() -> None:
    for row in ROWS:
        if row.kind == "none":
            assert row.why_not, f"{row.case}: a row nothing covers says why, or what it would take"
