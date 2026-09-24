"""When the nightly may merge its own reconciliation PR.

The cases worth a test are the ones that look safe and are not: a headline of
"no drift" over a diff that renames tools (#47), and a catalogue that did not
move on a night the lane was broken (#20600). Either must leave the PR for a
human.
"""

import json
from pathlib import Path

import pytest

from eval import reconcile as R
from eval.result_schema import Snapshot, ToolDef, Toolset

GREEN = {"static": "success", "admin-eval": "success", "store-snapshot": "success"}


def snap(*names: str, description: str = "d") -> Snapshot:
    tools = [ToolDef(name=n, description=description, inputSchema={}) for n in names]
    return Snapshot(
        server_instructions="hello",
        default_tools=["shopware-tool-search"],
        toolsets=[Toolset(name="entity", tools=list(names))],
        tools=tools,
    )


SAME = [("admin", snap("a", "b", "c"), snap("a", "b", "c"))]


def test_a_pin_bump_on_a_green_night_with_no_drift_merges() -> None:
    assert R.blockers(["shopware.sha"], SAME, GREEN) == []


def test_a_tightened_lint_budget_does_not_block() -> None:
    assert R.blockers(["shopware.sha", "tool-history/lint-budget.json"], SAME, GREEN) == []


def test_a_snapshot_in_the_diff_needs_a_review_whatever_the_headline_says() -> None:
    """#47: "No catalogue drift" on top, every UCP tool renamed in the file."""
    reasons = R.blockers(["shopware.sha", "tool-history/store.json"], SAME, GREEN)

    assert reasons == ["it changes `tool-history/store.json`, which needs a review"]


def test_drift_blocks_even_if_the_file_check_missed_it() -> None:
    drifted = [("admin", snap("a", "b", "c"), snap("a", "b", "c", description="reworded"))]

    assert R.blockers(["shopware.sha"], drifted, GREEN) == ["the admin catalogue drifted"]


def test_a_collapsed_catalogue_blocks() -> None:
    collapsed = [("store", snap("a", "b", "c", "d"), snap("a"))]

    assert R.blockers(["shopware.sha"], collapsed, GREEN) == ["the store catalogue collapsed"]


@pytest.mark.parametrize("job", ["static", "admin-eval"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_a_red_night_blocks_because_the_pin_would_move_prs_onto_it(job: str, result: str) -> None:
    """#20600: no description changed, and the lane could reach nothing."""
    reasons = R.blockers(["shopware.sha"], SAME, {**GREEN, job: result})

    assert reasons == [f"`{job}` did not pass on this Shopware commit ({result})"]


def test_an_unreported_job_blocks() -> None:
    assert R.blockers(["shopware.sha"], SAME, {"static": "success", "store-snapshot": "success"}) == [
        "`admin-eval` did not pass on this Shopware commit (missing)"
    ]


@pytest.mark.parametrize("outcome", ["failure", "skipped"])
def test_an_unmeasured_store_catalogue_blocks(outcome: str) -> None:
    """The step is continue-on-error and skipped without the plugin; either way
    the committed store.json is still on disk and compares equal to itself."""
    reasons = R.blockers(["shopware.sha"], SAME, {**GREEN, "store-snapshot": outcome})

    assert reasons == [f"`store-snapshot` did not pass on this Shopware commit ({outcome})"]


def test_the_store_eval_has_no_veto() -> None:
    """It is advisory everywhere else; a red Store run must not freeze the pin."""
    assert R.blockers(["shopware.sha"], SAME, {**GREEN, "store-eval": "failure"}) == []


def test_nothing_to_pin_blocks() -> None:
    assert R.blockers(["tool-history/lint-budget.json"], SAME, GREEN) == [
        "it does not move `shopware.sha`, so there is nothing to pin"
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def write(path: Path, snapshot: Snapshot) -> str:
    _ = path.write_text(json.dumps(snapshot))
    return str(path)


def test_cli_exits_zero_and_says_why_when_it_may_merge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = write(tmp_path / "old.json", snap("a"))
    new = write(tmp_path / "new.json", snap("a"))

    code = R.main(
        [
            "--staged",
            "shopware.sha",
            "--snapshot",
            "admin",
            old,
            new,
            "--job",
            "static=success",
            "--job",
            "admin-eval=success",
            "--job",
            "store-snapshot=success",
        ]
    )

    assert code == 0
    assert "Merged automatically" in capsys.readouterr().out


def test_cli_exits_one_with_every_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = write(tmp_path / "old.json", snap("a"))

    code = R.main(
        [
            "--staged",
            "shopware.sha",
            "--snapshot",
            "store",
            old,
            str(tmp_path / "absent.json"),
            "--job",
            "static=failure",
        ]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "the store snapshots could not be compared" in out
    assert "`static` did not pass on this Shopware commit (failure)" in out
    assert "`admin-eval` did not pass" in out
