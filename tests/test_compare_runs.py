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


def report(model, results):
    """results: list of (id, passed, expected_tool, skipped)"""
    return {
        "model": model,
        "modes": {
            "discovery": {
                "results": [{"id": i, "passed": p, "expected_tool": t, "skipped": s} for i, p, t, s in results]
            }
        },
    }


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


def test_step_summary_is_appended_when_ci_sets_it(tmp_path, monkeypatch, capsys):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(report("strong", [("f1", True, "t1", False)])))
    b.write_text(json.dumps(report("weak", [("f1", True, "t1", False)])))
    summary = tmp_path / "summary.md"

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(a), str(b)])

    assert C.main() == 0
    assert "Cross-model comparison" in summary.read_text()
    capsys.readouterr()


def test_unreadable_report_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(tmp_path / "nope.json"), str(tmp_path / "nope2.json")])
    assert C.main() == 2
    capsys.readouterr()
