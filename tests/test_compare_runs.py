"""Unit tests for the cross-model report comparison."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("eval_compare_runs", ROOT / "eval" / "compare_runs.py")
C = importlib.util.module_from_spec(_spec)
sys.modules["eval_compare_runs"] = C
_spec.loader.exec_module(C)


def report(model, results, errors=()):
    """results: list of (id, passed, expected_tool, skipped); errors: list of ids that errored"""
    errors = set(errors)
    records = []
    for i, p, t, s in results:
        rec = {"id": i, "passed": p, "expected_tool": t, "skipped": s}
        if i in errors:
            rec["error"] = "500 Server Error: Internal Server Error"
        records.append(rec)
    return {"model": model, "modes": {"discovery": {"results": records}}}


def test_splits_fixtures_four_ways():
    a = report(
        "strong",
        [("f1", True, "t1", False), ("f2", True, "t1", False), ("f3", False, "t2", False), ("f4", False, "t2", False)],
    )
    b = report(
        "weak",
        [("f1", True, "t1", False), ("f2", False, "t1", False), ("f3", True, "t2", False), ("f4", False, "t2", False)],
    )

    c = C.compare(a, b)

    assert c["both_pass"] == ["f1"]
    assert c["only_primary"] == ["f2"]
    assert c["only_second"] == ["f3"]
    assert c["both_fail"] == ["f4"]


def test_skipped_fixtures_are_excluded_from_rates():
    """Skipped fixtures never gate, so they must not dilute either rate."""
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", True)])
    b = report("weak", [("f1", True, "t1", False), ("f2", False, "t1", True)])

    c = C.compare(a, b)

    assert c["primary"]["total"] == 1
    assert c["primary"]["rate"] == 1.0
    assert c["shared"] == 1


def test_errored_fixtures_are_not_counted_as_failures():
    """The regression this guards: 18 server 500s once read as a 53% model score.

    Every fixture but one errors; the model got one right out of one that ran,
    so the rate is 100% with 3 errored — not 25%.
    """
    a = report(
        "strong",
        [("f1", True, "t1", False), ("f2", False, "t1", False), ("f3", False, "t1", False), ("f4", False, "t1", False)],
        errors=["f2", "f3", "f4"],
    )

    c = C.compare(a, a)

    assert c["primary"]["passed"] == 1
    assert c["primary"]["total"] == 1
    assert c["primary"]["rate"] == 1.0
    assert c["primary"]["errored"] == 3
    assert c["both_fail"] == []


def test_errored_fixtures_are_excluded_from_the_shared_comparison():
    """A fixture that errored for one model cannot be attributed to either."""
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", False)], errors=["f2"])
    b = report("weak", [("f1", True, "t1", False), ("f2", False, "t1", False)])

    c = C.compare(a, b)

    assert c["shared"] == 1
    assert c["both_fail"] == []
    assert c["only_primary"] == []
    # f2 errored, not mismatched — the fixture files still agree.
    assert c["unmatched"] == []


def test_render_warns_when_fixtures_errored():
    a = report("strong", [("f1", True, "t1", False), ("f2", False, "t1", False)], errors=["f2"])
    out = C.render(C.compare(a, a), 0.9)

    assert "never reached the model" in out
    assert "Errored" in out


def test_both_fail_is_grouped_by_tool():
    a = report("strong", [("f1", False, "t1", False), ("f2", False, "t1", False), ("f3", False, "t2", False)])
    b = report("weak", [("f1", False, "t1", False), ("f2", False, "t1", False), ("f3", False, "t2", False)])

    c = C.compare(a, b)

    assert c["both_fail_by_tool"] == {"t1": ["f1", "f2"], "t2": ["f3"]}


def test_unmatched_fixtures_are_flagged():
    """Different fixture sets between runs make the comparison meaningless."""
    a = report("strong", [("f1", True, "t1", False), ("only_in_a", True, "t1", False)])
    b = report("weak", [("f1", True, "t1", False), ("only_in_b", True, "t1", False)])

    c = C.compare(a, b)

    assert c["unmatched"] == ["only_in_a", "only_in_b"]
    assert c["shared"] == 1


def test_missing_discovery_mode_yields_empty_index():
    assert C.discovery_index({"modes": {"baseline": {"results": [{"id": "f1", "passed": True}]}}}) == {}
    assert C.pass_rate({}) == (0, 0, 0.0)


def test_render_lists_worst_tool_first():
    a = report("strong", [("f1", False, "lonely", False), ("f2", False, "busy", False), ("f3", False, "busy", False)])
    out = C.render(C.compare(a, a), 0.9)

    assert out.index("`busy`") < out.index("`lonely`")
    assert "3" in out


@pytest.mark.parametrize(
    "gate,strong_pass,weak_pass,expected",
    [
        ("none", False, False, 0),  # report only, never gates
        ("primary", True, False, 0),  # weak model below threshold is tolerated
        ("primary", False, True, 1),
        ("both", True, True, 0),
        ("both", True, False, 1),  # this is the "both over 90%" mode
    ],
)
def test_gate_modes(tmp_path, monkeypatch, capsys, gate, strong_pass, weak_pass, expected):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report("strong", [("f1", strong_pass, "t1", False)])))
    b.write_text(json.dumps(report("weak", [("f1", weak_pass, "t1", False)])))

    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(a), str(b), "--gate", gate])

    assert C.main() == expected
    capsys.readouterr()


def test_step_summary_is_left_to_the_summary_renderer(tmp_path, monkeypatch, capsys):
    """This script must not touch GITHUB_STEP_SUMMARY any more.

    eval/summary.py renders the job summary once, from --output. Appending here
    too would put a second copy of these tables in the middle of it.
    """
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report("strong", [("f1", True, "t1", False)])))
    b.write_text(json.dumps(report("weak", [("f1", True, "t1", False)])))
    summary = tmp_path / "summary.md"
    out_json = tmp_path / "cmp.json"

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(a), str(b), "--output", str(out_json)])

    assert C.main() == 0
    assert not summary.exists()
    assert json.loads(out_json.read_text())["shared"] == 1
    assert "Cross-model comparison" in capsys.readouterr().out


def test_both_fail_detail_records_what_each_model_picked():
    """The confusion pair is the finding — naming only the expected tool says a
    description is wrong but not what it lost to."""
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling", "fail_reason": "wrong_tool"}
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": None, "fail_reason": "no_tool_call"}

    detail = C.compare(a, b)["both_fail_detail"]

    assert detail == [
        {
            "id": "f1",
            "expected_tool": "wanted",
            "primary_selected": "sibling",
            "second_selected": None,
            "primary_reason": "wrong_tool",
            "second_reason": "no_tool_call",
        }
    ]


def test_both_fail_detail_degrades_when_fields_are_absent():
    """Older reports have no selected_tool/fail_reason; that must not raise."""
    a = report("strong", [("f1", False, "t1", False)])

    detail = C.compare(a, a)["both_fail_detail"]

    assert detail[0]["primary_selected"] is None
    assert detail[0]["primary_reason"] is None


def test_actionable_table_shows_both_picks_and_hides_wrong_tool():
    a = report("strong", [("f1", False, "wanted", False)])
    b = report("weak", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling_a", "fail_reason": "wrong_tool"}
    b["modes"]["discovery"]["results"][0] |= {"selected_tool": "sibling_b", "fail_reason": "wrong_tool"}

    out = C.render_actionable(C.compare(a, b), "strong", "weak")

    assert "`sibling_a`" in out
    assert "`sibling_b`" in out
    # Redundant with the two picked columns, so it is not printed.
    assert "wrong_tool" not in out


def test_actionable_table_keeps_reasons_the_columns_cannot_show():
    a = report("strong", [("f1", False, "wanted", False)])
    a["modes"]["discovery"]["results"][0] |= {"selected_tool": None, "fail_reason": "step_cap"}

    out = C.render_actionable(C.compare(a, a))

    assert "both: step_cap" in out
    assert "(none)" in out


def test_actionable_table_says_so_when_nothing_failed_twice():
    a = report("strong", [("f1", True, "t1", False)])
    assert "No fixture failed for both models." in C.render_actionable(C.compare(a, a))


def test_actionable_table_lists_one_row_per_fixture():
    a = report("strong", [("f1", False, "busy", False), ("f2", False, "busy", False), ("f3", False, "lonely", False)])

    out = C.render_actionable(C.compare(a, a))
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "|---" not in ln and "Expected tool" not in ln]

    assert len(rows) == 3
    assert out.index("`busy`") < out.index("`lonely`")


def test_actionable_table_puts_core_failures_above_plugin_failures():
    """Owner decides urgency: a core description problem outranks a plugin one
    however many prompts the plugin lost."""
    a = report(
        "strong",
        [
            ("p1", False, "merchant-order-summary", False),
            ("p2", False, "merchant-order-summary", False),
            ("c1", False, "shopware-entity-read", False),
        ],
    )

    out = C.render_actionable(C.compare(a, a))

    assert out.index("shopware-entity-read") < out.index("merchant-order-summary")


def test_actionable_table_names_the_owning_repository():
    a = report("strong", [("f1", False, "swag-dev-tools-load-skill", False)])

    out = C.render_actionable(C.compare(a, a))

    assert "| dev-tools |" in out


def test_unreadable_report_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")])
    assert C.main() == 2
    capsys.readouterr()
