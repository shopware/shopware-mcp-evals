"""Unit tests for the eval runner's per-run summary row.

The row is what eval/summary.py renders the job summary from, so its shape is a
contract between two files that never run in the same process.
"""

import json
from types import SimpleNamespace

from eval import runner as E


def args(summary_row=None, suite_label=None, advisory=False, endpoint="admin"):
    return SimpleNamespace(summary_row=summary_row, suite_label=suite_label, advisory=advisory, endpoint=endpoint)


def results(n_passed, n_errored=0, tool="shopware-entity-read"):
    out = [{"id": f"p{i}", "passed": True, "expected_tool": tool} for i in range(n_passed)]
    out += [
        {"id": f"e{i}", "passed": False, "error": "500 Server Error", "expected_tool": tool} for i in range(n_errored)
    ]
    return out


def test_row_carries_everything_the_summary_table_needs(tmp_path, capsys):
    path = tmp_path / "rows" / "1-admin-primary.json"

    row = E.write_summary_row(
        "openai",
        "gpt-4o",
        results(83, n_errored=2),
        0.92,
        True,
        args(summary_row=str(path), suite_label="admin · primary"),
    )
    capsys.readouterr()

    # The parent dir does not exist in CI until the first run writes into it.
    assert json.loads(path.read_text()) == row

    # Cost travels with the row so the summary can add its column without
    # re-reading every full report. Asserted separately: its contents are
    # eval/cost.py's contract, pinned in tests/test_cost.py, and inlining them
    # here would make an unrelated pricing edit fail this shape test.
    cost = row.pop("cost")
    assert cost["model"] == "gpt-4o" and cost["graded"] == 85

    assert row == {
        "suite": "admin · primary",
        "provider": "openai",
        "model": "gpt-4o",
        "rate": 0.92,
        "graded": 85,
        "errored": 2,
        "throttled": 0,
        "gate": "PASS",
        "advisory": False,
        # Errored fixtures are excluded here as they are from the rate, so the
        # per-tier numbers stay comparable with it. `ids` lists every graded
        # fixture so eval/summary.py can union rather than add when the same
        # fixture set is graded by more than one suite.
        "by_tier": {
            "core": {
                "passed": 83,
                "total": 83,
                "ids": [f"p{i}" for i in range(83)],
                "failed_ids": [],
                "rate": 1.0,
            }
        },
    }


def test_row_splits_by_owning_repository(capsys):
    mixed = [
        {"id": "c1", "passed": True, "expected_tool": "shopware-entity-read"},
        {"id": "d1", "passed": False, "expected_tool": "swag-dev-tools-load-skill"},
        {"id": "m1", "passed": True, "expected_tool": "merchant-order-summary"},
    ]

    row = E.write_summary_row("openai", "gpt-4o", mixed, 0.67, False, args())
    capsys.readouterr()

    assert row["by_tier"]["dev-tools"] == {
        "passed": 0,
        "total": 1,
        "ids": ["d1"],
        "failed_ids": ["d1"],
        "rate": 0.0,
    }
    assert row["by_tier"]["core"]["rate"] == 1.0
    assert list(row["by_tier"]) == ["core", "dev-tools", "merchant-tools"]


def test_failing_gate_is_recorded_as_fail(tmp_path, capsys):
    row = E.write_summary_row("openai", "gpt-4o", results(1), 0.5, False, args())
    capsys.readouterr()

    assert row["gate"] == "FAIL"


def test_advisory_flag_reaches_the_row(capsys):
    row = E.write_summary_row("openai", "gpt-4o", results(1), 1.0, True, args(advisory=True))
    capsys.readouterr()

    assert row["advisory"] is True


def test_suite_defaults_to_the_endpoint_when_unlabelled(capsys):
    row = E.write_summary_row("openai", "gpt-4o", results(1), 1.0, True, args(endpoint="store"))
    capsys.readouterr()

    assert row["suite"] == "store"


def test_no_file_is_written_without_summary_row(tmp_path, capsys):
    E.write_summary_row("openai", "gpt-4o", results(1), 1.0, True, args())

    assert list(tmp_path.iterdir()) == []
    # Still on stdout: the job summary only renders at the end of the job, so a
    # cancelled or timed-out run would otherwise report nothing anywhere.
    assert "Summary row:" in capsys.readouterr().out


def test_a_run_with_no_results_still_produces_a_row(capsys):
    """A row is how the job summary learns a suite ran at all, so it must survive
    an empty result set rather than raising on len(None)."""
    row = E.write_summary_row("openai", "gpt-4o", None, 0.0, False, args())
    capsys.readouterr()

    assert row["graded"] == 0 and row["gate"] == "FAIL"


def test_throttled_fixtures_are_counted(capsys):
    throttled = [{"id": "t1", "passed": False, "error": "Error code: 429 rate limit"}]
    row = E.write_summary_row("openai", "gpt-4o", throttled, 0.0, False, args())

    assert row["throttled"] == 1
    assert "hit provider rate limits" in capsys.readouterr().out
