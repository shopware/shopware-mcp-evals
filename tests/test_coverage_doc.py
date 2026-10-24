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


def label_literals(directory: Path) -> set[str]:
    """Every string constant in the modules, docstrings left out.

    f-strings count by their literal parts, which is all a runtime-built label
    has in the source. A docstring is prose about a check, not a check.
    """
    found: set[str] = set()
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        found.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        )
    return found


def workflow_jobs(text: str) -> set[str]:
    body = text.split("\njobs:\n", 1)[1]
    return set(re.findall(r"^  ([a-z][a-z0-9-]*):$", body, re.MULTILINE))


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


def test_every_gap_says_why() -> None:
    for row in ROWS:
        if row.kind == "none":
            assert row.why_not, f"{row.case}: a row nothing covers says why, or what it would take"
