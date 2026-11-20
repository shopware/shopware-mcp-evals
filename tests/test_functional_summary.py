"""The functional part of the job summary: by lane, by caller, and what differs."""

import json
import sys
from pathlib import Path
from typing import cast

import pytest

from eval import functional_summary as F
from eval import summary as S
from eval.result_schema import FunctionalRecord, FunctionalReport, JsonObject, Report

KNOWN = "shopware/agentic-commerce#163"


def rec(label: str, status: str = "pass", principal: str | None = "admin", **extra: str) -> FunctionalRecord:
    record: JsonObject = {"tool": "check", "label": label, "status": status, **extra}
    if principal is not None:
        record["principal"] = principal
    return cast(FunctionalRecord, cast(object, record))


def report(records: list[FunctionalRecord], lane: str | None, endpoint: str = "admin", **extra: object) -> JsonObject:
    passed = sum(r["status"] == "pass" for r in records)
    failed = sum(r["status"] == "fail" for r in records)
    skipped = sum(r["status"] == "skipped" for r in records)
    out: JsonObject = {
        "timestamp": "2026-10-23T00:00:00+00:00",
        "server": "http://lane",
        "pass": passed,
        "fail": failed,
        "skip": skipped,
        "total": passed + failed + skipped,
        "tools": cast(list[object], records),
        "health": {},
        "endpoint": endpoint,
        "protocol_version": lane if lane in ("2024-11-05", "2025-06-18") else "2025-11-25",
        **extra,
    }
    if lane is not None:
        out["lane"] = lane
    return out


def run_of(data: JsonObject) -> F.LaneRun:
    lane = data.get("lane")
    return F.LaneRun(
        lane if isinstance(lane, str) else None,
        str(data.get("endpoint", "?")),
        cast(FunctionalReport, cast(object, data)),
    )


def admin_records(*, fail_on: str = "") -> list[FunctionalRecord]:
    return [
        rec("default surface", "fail" if fail_on == "default surface" else "pass"),
        rec("toolset-enable"),
        rec("dev tools (--skip-dev-tools)", "skipped"),
        rec("integration-all: reaches everything", principal="allowlist"),
        rec("oauth-user-one-tool: reaches one tool", principal="oauth"),
        rec("MCP OAuth flow", principal="oauth"),
    ]


def store_records() -> list[FunctionalRecord]:
    return [
        rec("ucp toolset deferred"),
        rec("a signed-in buyer can place a second order", "fail", known_issue=KNOWN, error="Completed"),
    ]


PER_REVISION_SKIP = "result format, large results, stateless era, OAuth flow"


def three_lanes(*, fail_2024: bool = False) -> list[F.LaneRun]:
    revision_admin = [
        r for r in admin_records(fail_on="default surface" if fail_2024 else "") if "OAuth" not in r["label"]
    ]
    revision_admin = [r for r in revision_admin if r.get("principal") != "oauth"]
    revision_admin.append(rec(PER_REVISION_SKIP, "skipped", reason="compared once"))
    return [
        run_of(report(admin_records(), "default")),
        run_of(report(store_records(), "default", "store")),
        run_of(report(revision_admin, "2024-11-05")),
        run_of(report(store_records(), "2024-11-05", "store")),
        run_of(report([r for r in admin_records() if r.get("principal") == "admin"], "redis")),
        run_of(report([rec("admin: key appears on initialize")], "redis", "sessions")),
    ]


def rows_of(markdown: str) -> list[list[str]]:
    return [[c.strip() for c in line.strip("|").split("|")] for line in markdown.splitlines() if line.startswith("| ")]


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def test_three_lanes_render_one_row_per_endpoint_and_caller() -> None:
    md = F.render_functional(three_lanes())

    rows = rows_of(md)
    assert rows[0] == ["Lane", "Endpoint", "Caller", "Passed", "New failures", "Expected failures", "Skipped"]
    body = rows[1:]
    link = f"1 ([{KNOWN}](https://github.com/shopware/agentic-commerce/issues/163))"
    assert body == [
        ["default · 2025-11-25", "admin", "suite key", "2", "0", "0", "1"],
        ["", "admin", "allowlist principals", "1", "0", "0", "0"],
        ["", "admin", "OAuth", "2", "0", "0", "0"],
        ["", "store", "suite key", "1", "0", link, "0"],
        ["2024-11-05", "admin", "suite key", "2", "0", "0", "2"],
        ["", "admin", "allowlist principals", "1", "0", "0", "0"],
        ["", "store", "suite key", "1", "0", link, "0"],
        ["Redis sessions · 2025-11-25", "admin", "suite key", "2", "0", "0", "1"],
        ["", "sessions", "suite key", "1", "0", "0", "0"],
    ]


def test_a_new_failure_is_bold_and_counted_apart_from_a_known_one() -> None:
    md = F.render_functional(three_lanes(fail_2024=True))

    row = next(r for r in rows_of(md) if r[0] == "2024-11-05")
    assert row[3:] == ["1", "**1**", "0", "2"]


def test_an_unawaited_lane_without_a_report_reads_not_finished() -> None:
    """next-major is not in `report`'s needs: still running is not the same as broken."""
    md = F.render_functional(three_lanes(), ["default", "2024-11-05", "2025-06-18", "6.8", "redis"], ["6.8"])

    rows = {r[0]: r for r in rows_of(md)}
    assert rows["6.8 (V6_8_0_0)"][1] == "not finished"
    assert rows["2025-06-18"][1] == "no report"
    lanes = [r[0] for r in rows_of(md)[1:] if r[0]]
    assert lanes == [
        "default · 2025-11-25",
        "2024-11-05",
        "2025-06-18",
        "6.8 (V6_8_0_0)",
        "Redis sessions · 2025-11-25",
    ]


def test_nothing_to_render_without_reports_or_lanes() -> None:
    assert F.render_functional([]) == ""


def test_a_report_that_recorded_nothing_is_still_a_row() -> None:
    md = F.render_functional([run_of(report([], "6.8"))])

    assert rows_of(md)[1] == ["6.8 (V6_8_0_0) · 2025-11-25", "admin", "suite key", "0", "0", "0", "0"]


def test_two_reports_for_one_lane_and_endpoint_add_up() -> None:
    runs = [run_of(report([rec("a")], "default")), run_of(report([rec("b"), rec("c", "skipped")], "default"))]

    assert rows_of(F.render_functional(runs))[1][3:] == ["2", "0", "0", "1"]


# ---------------------------------------------------------------------------
# Older reports
# ---------------------------------------------------------------------------
def test_a_report_without_lane_or_callers_is_counted_from_its_totals() -> None:
    """Before `principal`, bare skips were not recorded, so the records would
    undercount; the report's own totals are the honest number."""
    records = [
        rec("a", principal=None),
        rec("b", "fail", principal=None, known_issue=KNOWN),
        rec("c", "fail", principal=None),
    ]
    data = report(records, None)
    data["skip"] = 4
    data["expected_fail"] = 1
    del data["endpoint"]

    md = F.render_functional([F.LaneRun(None, "store", cast(FunctionalReport, cast(object, data)))])

    assert rows_of(md)[1][:2] == ["(not recorded)", "store"]
    assert rows_of(md)[1][2:5] == ["all callers (not recorded)", "1", "**1**"]
    assert rows_of(md)[1][5].startswith("1 ([shopware/agentic-commerce#163]")
    assert rows_of(md)[1][6] == "4"


def test_an_old_report_with_only_bare_skips_is_not_empty() -> None:
    data = report([], None)
    data["skip"] = 2
    data["total"] = 2

    md = F.render_functional([run_of(data)])

    assert rows_of(md)[1][2:] == ["all callers (not recorded)", "0", "0", "0", "2"]


def test_load_reads_every_lane_directory_and_falls_back_to_the_filename(tmp_path: Path) -> None:
    lane = tmp_path / "functional-2024-11-05"
    lane.mkdir()
    (lane / "functional-store-20261023-010203.json").write_text(json.dumps(report([rec("a")], "2024-11-05", "store")))
    old = report([rec("b", principal=None)], None)
    del old["endpoint"]
    (tmp_path / "functional-default").mkdir()
    (tmp_path / "functional-default" / "functional-admin-20260101-000000.json").write_text(json.dumps(old))
    (tmp_path / "functional-odd.json").write_text(json.dumps(old))

    runs = F.load_functional(tmp_path)

    assert sorted((r.lane or "", r.endpoint) for r in runs) == [("", "?"), ("", "admin"), ("2024-11-05", "store")]


def test_load_survives_a_missing_directory_and_a_broken_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "functional-admin-1.json").write_text("{not json")

    assert F.load_functional(None) == []
    assert F.load_functional(tmp_path / "nope") == []
    assert F.load_functional(tmp_path) == []
    assert "Could not read functional report" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Differs by lane
# ---------------------------------------------------------------------------
def test_lanes_that_agree_are_one_line() -> None:
    """Including the per-revision skip and the sections that run on some lanes
    only: neither is a difference in the server."""
    md = F.render_functional(three_lanes())

    [line] = [line for line in md.splitlines() if "Differs by lane" in line]
    assert line.endswith("Every check that ran on more than one lane had the same result on each.")
    assert md.rstrip().endswith(line)


def test_a_check_that_fails_on_one_lane_is_listed_with_each_lanes_result() -> None:
    md = F.render_differs(three_lanes(fail_2024=True))

    assert md.splitlines() == [
        "**Differs by lane:** 1 check(s).",
        "",
        "- admin · `default surface`: pass on default, Redis sessions; fail on 2024-11-05",
    ]


def test_several_records_under_one_label_count_as_the_worst() -> None:
    a = run_of(report([rec("x"), rec("x", "skipped")], "default"))
    b = run_of(report([rec("x", "skipped")], "6.8"))

    assert F.differs_by_lane([a, b]) == []


def test_a_report_without_a_lane_is_not_compared() -> None:
    runs = [run_of(report([rec("x")], "default")), run_of(report([rec("x", "fail")], None))]

    assert F.render_differs(runs).endswith("fewer than two lanes reported.")


def test_a_long_list_is_capped() -> None:
    labels = [f"check {i:02}" for i in range(F.DIFFERS_SHOWN + 3)]
    runs = [
        run_of(report([rec(label) for label in labels], "default")),
        run_of(report([rec(label, "fail") for label in labels], "2025-06-18")),
    ]

    md = F.render_differs(runs)

    assert md.splitlines()[0] == f"**Differs by lane:** {len(labels)} check(s)."
    assert md.splitlines()[-1] == "- … and 3 more in the functional reports."


def test_an_unrecognised_status_is_ignored() -> None:
    runs = [run_of(report([rec("x", "weird")], "default")), run_of(report([rec("x")], "redis"))]

    assert F.differs_by_lane(runs) == []


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------
def test_issue_links() -> None:
    assert F.issue_link(KNOWN) == f"[{KNOWN}](https://github.com/shopware/agentic-commerce/issues/163)"
    assert F.issue_link("org/repo#7") == "[org/repo#7](https://github.com/org/repo/issues/7)"
    assert F.issue_link("not an issue") == "not an issue"


def test_an_unknown_lane_and_endpoint_sort_after_the_known_ones() -> None:
    runs = [run_of(report([rec("x")], "custom", "other")), run_of(report([rec("x")], "default"))]

    lanes = [r[0] for r in rows_of(F.render_functional(runs))[1:]]
    assert lanes == ["default · 2025-11-25", "custom · 2025-11-25"]


@pytest.mark.parametrize(
    ("revisions", "line"),
    [
        ([], ""),
        (["2025-11-25"], "LLM eval ran at revision 2025-11-25.\n"),
        (["2025-11-25", "2024-11-05"], "LLM eval ran at revisions 2024-11-05, 2025-11-25.\n"),
        ([None], "LLM eval revision: not recorded in its reports.\n"),
    ],
)
def test_the_eval_revision_line(revisions: list[str | None], line: str) -> None:
    reports = [cast(Report, cast(object, {} if r is None else {"protocol_version": r})) for r in revisions]

    assert F.render_eval_revision(reports) == line


# ---------------------------------------------------------------------------
# Wired into the page
# ---------------------------------------------------------------------------
def test_main_renders_the_lanes_and_the_eval_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    functional = tmp_path / "functional-reports"
    for data in (report([rec("x")], "default"), report([rec("x")], "2025-06-18")):
        lane_dir = functional / f"functional-{data['lane']}"
        lane_dir.mkdir(parents=True)
        (lane_dir / "functional-admin-1.json").write_text(json.dumps(data))
    eval_report = tmp_path / "eval-primary.json"
    eval_report.write_text(json.dumps({"protocol_version": "2025-11-25", "modes": {}}))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["summary.py", "--rows", str(tmp_path / "rows"), "--tools", str(tmp_path / "none.json")]
    argv += ["--reports", str(eval_report), "--functional", str(functional)]
    argv += ["--functional-lanes", "default", "2025-06-18", "6.8", "--functional-unawaited", "6.8"]
    monkeypatch.setattr(sys, "argv", argv)

    assert S.main() == 0

    out = capsys.readouterr().out
    assert "LLM eval ran at revision 2025-11-25." in out
    assert "### Functional suite, by lane" in out
    assert "| 6.8 (V6_8_0_0) | not finished |" in out
    assert "Every check that ran on more than one lane had the same result on each." in out


def test_without_functional_reports_the_page_has_no_lane_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["summary.py", "--rows", str(tmp_path), "--tools", str(tmp_path / "x.json")])

    assert S.main() == 0
    assert "Functional suite" not in capsys.readouterr().out
