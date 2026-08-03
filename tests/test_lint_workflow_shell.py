"""The linter that covers the shell inside workflow `run:` blocks.

Its own scaffolding was the hard part, and each of these pins a mistake made while
writing it: a substitution that invented findings, a declaration that reported
itself, and a default severity at which a planted bug sailed through.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lint_workflow_shell as L  # noqa: E402 — needs the path above

ROOT = Path(__file__).resolve().parents[1]


def test_the_repos_own_workflow_shell_is_clean() -> None:
    """The gate starts green, which is why it can be gating rather than advisory.
    If this fails, something new was added that ShellCheck objects to."""
    result = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, "scripts/lint_workflow_shell.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_gha_expression_does_not_invent_findings() -> None:
    """`${{ }}` is not shell. Substituting a literal makes every
    `[ "${{ x }}" = "y" ]` a constant comparison (SC2050); substituting a quoted
    token makes it `""$GHA_EXPR""` (SC2027). Neither is in the workflow."""
    script = L.as_script('if [ "${{ inputs.event_name }}" = "schedule" ]; then :; fi\n', pipefail=False)

    assert '""' not in script, "a quoted substitution would double the quotes"
    assert '"$GHA_EXPR"' in script, "the YAML's own quoting has to survive"


def test_the_helper_variable_is_only_declared_when_used() -> None:
    """Declaring it unconditionally reported SC2034 on every block that has no
    expression — 35 findings, all of them the linter's own scaffolding."""
    with_expression = L.as_script('echo "${{ inputs.x }}"\n', pipefail=False)
    without = L.as_script("echo plain\n", pipefail=False)

    assert "GHA_EXPR=x" in with_expression
    assert "GHA_EXPR" not in without


def test_the_shell_options_match_what_github_runs() -> None:
    """A composite action's `shell: bash` is `-eo pipefail`; a workflow `run:` is
    `bash -e`. Getting it wrong reports failures that cannot happen, or misses
    ones that can."""
    assert "set -eo pipefail" in L.as_script("true\n", pipefail=True)
    assert "set -e\n" in L.as_script("true\n", pipefail=False)


def test_the_default_severity_catches_an_unquoted_expansion(tmp_path: Path) -> None:
    """The regression that matters. The first version defaulted to `warning`, and a
    planted `echo $unquoted` passed — SC2086 is info. A gate that passes a real bug
    is worse than no gate, because it reads as coverage."""
    # `t=$1`, not `t=1`: ShellCheck proves a literal assignment is safe to expand
    # unquoted and rightly stays silent, so a plant using one tests nothing. The
    # value has to be something it cannot see into.
    workflow = tmp_path / "planted.yml"
    workflow.write_text("jobs:\n  j:\n    steps:\n      - run: |\n          t=$1\n          echo $t\n")

    result = subprocess.run(  # noqa: S603 — fixed argv
        [sys.executable, str(ROOT / "scripts" / "lint_workflow_shell.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, f"the planted SC2086 was not caught:\n{result.stdout}"
    assert "SC2086" in result.stdout


def test_both_workflow_and_composite_action_shapes_are_read(tmp_path: Path) -> None:
    """A workflow keeps steps under `jobs.<id>.steps`, a composite action under
    `runs.steps`. Reading only one shape would silently skip every action — which
    is where the lane setup lives, the largest shell in the repo."""
    (tmp_path / "wf.yml").write_text("jobs:\n  build:\n    steps:\n      - name: a\n        run: echo a\n")
    (tmp_path / "action.yml").write_text("runs:\n  steps:\n    - name: b\n      run: echo b\n")

    labels = [label for p in sorted(tmp_path.glob("*.yml")) for label, _ in L.run_blocks(p)]

    assert any("build" in label for label in labels), "workflow shape"
    assert any("runs" in label for label in labels), "composite action shape"


@pytest.mark.parametrize("document", ["", "jobs: {}", "runs:\n  using: composite\n", "[]"])
def test_a_file_with_no_run_blocks_contributes_nothing(document: str, tmp_path: Path) -> None:
    """A linter that raises on an unfamiliar file is a linter people stop running."""
    path = tmp_path / "x.yml"
    path.write_text(document)

    assert L.run_blocks(path) == []
