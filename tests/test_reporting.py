"""Unit tests for the functional reporting harness."""

import json

from functional.reporting import Reporter


def test_counts_and_exit_code():
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "why")
    rep.tool_pass("shopware-x", "c", "preview text")
    rep.tool_fail("shopware-y", "d", "err")
    rep.skip("e")
    assert (rep.passed, rep.failed, rep.skipped, rep.total) == (2, 2, 1, 5)
    assert rep.exit_code == 1


def test_exit_code_zero_when_no_failures():
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.skip("b")
    assert rep.exit_code == 0


def test_record_shapes_match_bash_schema():
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


def test_write_report_roundtrip(tmp_path):
    rep = Reporter("srv", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "why")
    rep.skip("c")
    out = tmp_path / "sub" / "report.json"
    rep.write_report(out)  # also creates the parent directory
    report = json.loads(out.read_text())
    assert set(report) == {"timestamp", "server", "pass", "fail", "skip", "total", "tools"}
    assert (report["pass"], report["fail"], report["skip"], report["total"]) == (1, 1, 1, 3)
    assert report["server"] == "srv"
    assert len(report["tools"]) == 2


def test_color_disabled_emits_no_ansi(capsys):
    rep = Reporter("srv", color=False)
    rep.check_pass("clean")
    assert "\033" not in capsys.readouterr().out


def test_banner_section_and_info_print_their_text(capsys):
    rep = Reporter("admin", color=False)

    rep.banner("Shopware MCP Functional Tests")
    rep.section("v2: Toolset taxonomy")
    rep.info("Session: abc")
    out = capsys.readouterr().out

    assert "Shopware MCP Functional Tests" in out
    assert "v2: Toolset taxonomy" in out
    assert "Session: abc" in out


def test_summary_reports_all_three_counts_and_the_total(capsys):
    rep = Reporter("admin", color=False)
    rep.check_pass("a")
    rep.check_fail("b", "boom")
    rep.skip("c")

    rep.summary()
    out = capsys.readouterr().out

    assert "1 passed" in out and "1 failed" in out and "1 skipped" in out
    assert "/ 3 total" in out
