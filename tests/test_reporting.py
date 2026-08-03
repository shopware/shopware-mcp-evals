"""Unit tests for the functional reporting harness."""

import json
from pathlib import Path
from typing import cast

import pytest

from eval.result_schema import JsonObject, as_list
from functional.reporting import Reporter


def test_counts_and_exit_code() -> None:
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "why")
    rep.tool_pass("shopware-x", "c", "preview text")
    rep.tool_fail("shopware-y", "d", "err")
    rep.skip("e")
    assert (rep.passed, rep.failed, rep.skipped, rep.total) == (2, 2, 1, 5)
    assert rep.exit_code == 1


def test_exit_code_zero_when_no_failures() -> None:
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.skip("b")
    assert rep.exit_code == 0


def test_record_shapes_match_bash_schema() -> None:
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "why")
    rep.tool_pass("shopware-x", "c", "preview text")
    rep.tool_fail("shopware-y", "d", "err")
    rep.skip("e")  # skips are counted, never recorded
    assert rep.records == [
        {"tool": "check", "label": "a", "status": "pass"},
        {"tool": "check", "label": "b", "status": "fail", "error": "why"},
        {"tool": "shopware-x", "label": "c", "status": "pass", "preview": "preview text"},
        {"tool": "shopware-y", "label": "d", "status": "fail", "error": "err"},
    ]


def test_write_report_roundtrip(tmp_path: Path) -> None:
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "why")
    rep.skip("c")
    out = tmp_path / "sub" / "report.json"
    rep.write_report(out)  # also creates the parent directory
    report = cast(JsonObject, cast(object, json.loads(out.read_text())))
    assert set(report) == {"timestamp", "server", "pass", "fail", "skip", "total", "tools", "health"}
    assert (report["pass"], report["fail"], report["skip"], report["total"]) == (1, 1, 1, 3)
    assert report["server"] == "srv"
    assert len(as_list(report["tools"])) == 2
    # Structural checks describe the server, not a tool, so they contribute no
    # health entries — there are no fixtures to gate on them.
    assert report["health"] == {}


def test_tool_health_takes_the_worst_status_per_tool() -> None:
    """A tool asserted several times is healthy only if every assertion held. The
    gate asks "is there any reason not to trust this tool", not "did it ever work
    once" — a tool that passes one payload and fails another is not safe to spend
    LLM budget on."""
    rep = Reporter("srv", color=False)
    rep.tool_pass("shopware-x", "first payload")
    rep.tool_fail("shopware-x", "second payload", "boom")
    rep.tool_pass("shopware-y", "only payload")
    rep.tool_skip("shopware-z", "unsafe tool", "no dryRun")

    health = rep.tool_health()

    assert health["shopware-x"] == {"status": "fail", "reason": "boom"}
    assert health["shopware-y"] == {"status": "pass"}
    assert health["shopware-z"] == {"status": "skipped", "reason": "no dryRun"}


def test_a_skip_is_recorded_rather_than_only_counted() -> None:
    """A skip that leaves no record is indistinguishable from a pass downstream,
    which is how a suite starts overstating its own coverage."""
    rep = Reporter("srv", color=False)
    rep.tool_skip("shopware-x", "label", "because")

    assert rep.skipped == 1
    assert rep.records == [{"tool": "shopware-x", "label": "label", "status": "skipped", "reason": "because"}]


def test_write_health_is_a_standalone_artifact(tmp_path: Path) -> None:
    """The eval job consumes this across a job boundary, so it needs its own
    stable file rather than a field inside a timestamped report."""
    rep = Reporter("srv", color=False)
    rep.tool_fail("shopware-x", "label", "boom")
    out = tmp_path / "nested" / "tool-health.json"
    rep.write_health(out)

    assert json.loads(out.read_text()) == {"shopware-x": {"status": "fail", "reason": "boom"}}


def test_color_disabled_emits_no_ansi(capsys: pytest.CaptureFixture[str]) -> None:
    rep = Reporter("srv", color=False)
    rep.check_pass("clean")
    assert "\033" not in capsys.readouterr().out


def test_banner_section_and_info_print_their_text(capsys: pytest.CaptureFixture[str]) -> None:
    rep = Reporter("admin", color=False)

    rep.banner("Shopware MCP Functional Tests")
    rep.section("v2: Toolset taxonomy")
    rep.info("Session: abc")
    out = capsys.readouterr().out

    assert "Shopware MCP Functional Tests" in out
    assert "v2: Toolset taxonomy" in out
    assert "Session: abc" in out


def test_summary_reports_all_three_counts_and_the_total(capsys: pytest.CaptureFixture[str]) -> None:
    rep = Reporter("admin", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "boom")
    rep.skip("c")

    rep.summary()
    out = capsys.readouterr().out

    assert "1 passed" in out and "1 failed" in out and "1 skipped" in out
    assert "/ 3 total" in out
