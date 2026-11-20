"""The functional part of the job summary: every lane, split by who called.

The functional suite runs in several CI jobs, one per lane (functional/reporting.py
LANES): `static` at the default revision, the `revisions` matrix, `next-major`
under V6_8_0_0 and `session-store` on Redis. Each writes its reports into an
artifact of its own, `functional-<lane>`, and the report job downloads those into
one directory, which is what `load_functional` reads. Not the merged `results/`:
two lanes finishing in the same second write the same timestamped filename, and
the eval jobs re-upload static's reports inside their own artifacts.

The lane and the caller come from the report (`lane`) and each record
(`principal`), never from labels. A report from before those fields existed is
still counted: under `(not recorded)` for its lane, and as one row with every
caller in it, from the report's own totals.

Pure: eval/summary.py calls `render_functional` and owns the page.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from eval.result_schema import FunctionalRecord, FunctionalReport, Report
from functional.known_failures import KNOWN_FAILURES
from functional.reporting import LANES, PRINCIPALS

LANE_TITLES = {
    "default": "default",
    "2024-11-05": "2024-11-05",
    "2025-06-18": "2025-06-18",
    "6.8": "6.8 (V6_8_0_0)",
    "redis": "Redis sessions",
}
CALLER_TITLES = {"admin": "suite key", "allowlist": "allowlist principals", "oauth": "OAuth"}
ENDPOINT_ORDER = ("admin", "store", "sessions")
UNRECORDED = "(not recorded)"
# A record or report with no caller: written before `principal` existed.
ALL_CALLERS = "all callers (not recorded)"
# Enough to name a pattern; a longer list means something broke a whole lane,
# and the table already says which.
DIFFERS_SHOWN = 15

_RANK = {"pass": 0, "skipped": 1, "fail": 2}
_ISSUE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_FILENAME = re.compile(r"^functional-([a-z_]+)-")


@dataclass(frozen=True)
class LaneRun:
    """One report, with the two things the table is keyed on."""

    lane: str | None
    endpoint: str
    report: FunctionalReport


@dataclass
class Tally:
    passed: int = 0
    new: int = 0
    expected: int = 0
    skipped: int = 0
    issues: list[str] = field(default_factory=list)

    def add_issue(self, issue: str) -> None:
        if issue and issue not in self.issues:
            self.issues.append(issue)


def load_functional(root: Path | None) -> list[LaneRun]:
    """Every `functional-*.json` under `root`, at any depth.

    A missing directory is no reports, and an unreadable report is a warning,
    never a failed build: the summary is a reporting convenience.
    """
    if root is None or not root.is_dir():
        return []
    runs: list[LaneRun] = []
    for path in sorted(root.rglob("functional-*.json")):
        try:
            report = cast(FunctionalReport, json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::warning::Could not read functional report {path}: {exc}", file=sys.stderr)
            continue
        endpoint = report.get("endpoint")
        if not endpoint:
            match = _FILENAME.match(path.name)
            endpoint = match.group(1) if match else "?"
        runs.append(LaneRun(report.get("lane") or None, endpoint, report))
    return runs


def _lane_order(lane: str | None) -> tuple[int, str]:
    if lane is None:
        return (len(LANES) + 1, "")
    return (LANES.index(lane), lane) if lane in LANES else (len(LANES), lane)


def _endpoint_order(endpoint: str) -> tuple[int, str]:
    return (ENDPOINT_ORDER.index(endpoint), endpoint) if endpoint in ENDPOINT_ORDER else (len(ENDPOINT_ORDER), endpoint)


def _caller_order(caller: str) -> int:
    return PRINCIPALS.index(caller) if caller in PRINCIPALS else len(PRINCIPALS)


def _legacy(report: FunctionalReport) -> bool:
    """Written before records carried `principal`, so its callers cannot be split."""
    records = report.get("tools") or []
    if not records:
        return report.get("total", 0) > 0
    return any("principal" not in r for r in records)


def _tallies(run: LaneRun) -> dict[str, Tally]:
    report = run.report
    records = report.get("tools") or []
    if _legacy(report):
        # The report's own totals, because an older reporter did not record its
        # bare skips: counting records would understate them.
        expected = report.get("expected_fail", 0)
        tally = Tally(
            passed=report.get("pass", 0),
            new=report.get("fail", 0) - expected,
            expected=expected,
            skipped=report.get("skip", 0),
        )
        for record in records:
            tally.add_issue(record.get("known_issue", ""))
        return {ALL_CALLERS: tally}
    out: dict[str, Tally] = {}
    for record in records:
        tally = out.setdefault(record.get("principal", "admin"), Tally())
        _count(tally, record)
    # A run that recorded nothing still ran: one row of zeros says so.
    return out or {"admin": Tally()}


def _count(tally: Tally, record: FunctionalRecord) -> None:
    status = record.get("status")
    if status == "pass":
        tally.passed += 1
    elif status == "skipped":
        tally.skipped += 1
    elif status == "fail":
        if issue := record.get("known_issue"):
            tally.expected += 1
            tally.add_issue(issue)
        else:
            tally.new += 1


def issue_link(issue: str) -> str:
    """`owner/repo#n` as a link, the registry's URL when it still has the entry."""
    url = next((k.url for k in KNOWN_FAILURES if k.issue == issue), "")
    if not url and (match := _ISSUE.match(issue)):
        url = f"https://github.com/{match.group(1)}/issues/{match.group(2)}"
    return f"[{issue}]({url})" if url else issue


def _lane_title(lane: str | None, revisions: set[str]) -> str:
    if lane is None:
        return UNRECORDED
    title = LANE_TITLES.get(lane, lane)
    shown = sorted(r for r in revisions if r != lane)
    return f"{title} · {', '.join(shown)}" if shown else title


def render_functional(
    runs: list[LaneRun], expected_lanes: list[str] | None = None, unawaited: list[str] | None = None
) -> str:
    """One table, a row per lane, endpoint and caller, then what differs by lane.

    `expected_lanes` are the lanes this run started. One without a report still
    gets a row, so a lane that died says so instead of vanishing: "not
    finished" for a lane in `unawaited` (next-major, which the report job does
    not wait for, so it may simply still be running), "no report" for the rest.
    """
    expected_lanes = expected_lanes or []
    unawaited = unawaited or []
    if not runs and not expected_lanes:
        return ""
    by_lane: dict[str | None, list[LaneRun]] = {}
    for run in runs:
        by_lane.setdefault(run.lane, []).append(run)
    lanes = sorted({*by_lane, *expected_lanes}, key=_lane_order)

    lines = [
        "### Functional suite, by lane",
        "",
        "| Lane | Endpoint | Caller | Passed | New failures | Expected failures | Skipped |",
        "|---|---|---|---:|---:|---|---:|",
    ]
    for lane in lanes:
        lane_runs = by_lane.get(lane, [])
        revisions = {r.report.get("protocol_version", "") for r in lane_runs} - {""}
        title = _lane_title(lane, revisions)
        if not lane_runs:
            state = "not finished" if lane in unawaited else "no report"
            lines.append(f"| {title} | {state} | | | | | |")
            continue
        rows: dict[tuple[str, str], Tally] = {}
        for run in lane_runs:
            for caller, tally in _tallies(run).items():
                into = rows.setdefault((run.endpoint, caller), Tally())
                into.passed += tally.passed
                into.new += tally.new
                into.expected += tally.expected
                into.skipped += tally.skipped
                for issue in tally.issues:
                    into.add_issue(issue)
        first = True
        for endpoint, caller in sorted(rows, key=lambda k: (_endpoint_order(k[0]), _caller_order(k[1]))):
            t = rows[(endpoint, caller)]
            new = f"**{t.new}**" if t.new else "0"
            expected = str(t.expected) + (f" ({', '.join(issue_link(i) for i in t.issues)})" if t.issues else "")
            lines.append(
                f"| {title if first else ''} | {endpoint} | {CALLER_TITLES.get(caller, caller)} | "
                f"{t.passed} | {new} | {expected} | {t.skipped} |"
            )
            first = False
    lines += ["", render_differs(runs), ""]
    return "\n".join(lines)


def differs_by_lane(runs: list[LaneRun]) -> list[tuple[str, str, dict[str, str]]]:
    """Checks whose result changes between lanes: (endpoint, label, lane -> status).

    Compared only where a check ran on two lanes or more. A label that exists on
    some lanes only is left out on purpose, because that is what the lanes are
    built to do, not a difference in the server: the checks that span revisions
    (result format, large results, stateless era, OAuth flow) run on the
    default-revision lanes and are one skip on the others, Redis runs no Store
    suite and no principals, and a check that appends detail to its label on a
    pass ("… (order abc)") is a different string from its failure. Listing those
    would bury the one line a reader came for, under every check of a section
    that ran once. A failure on a lane of its own is still counted in the table.

    Several records under one label on one lane (two callers, say) collapse to the
    worst: fail, then skipped, then pass. A report with no `lane` is not
    compared, since nothing says which lane it would differ from.
    """
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for run in runs:
        if run.lane is None:
            continue
        for record in run.report.get("tools") or []:
            status = record.get("status", "")
            if status not in _RANK:
                continue
            by_lane = seen.setdefault((run.endpoint, record.get("label", "")), {})
            current = by_lane.get(run.lane)
            if current is None or _RANK[status] > _RANK[current]:
                by_lane[run.lane] = status
    out = [
        (endpoint, label, by_lane)
        for (endpoint, label), by_lane in seen.items()
        if len(by_lane) >= 2 and len(set(by_lane.values())) > 1
    ]
    return sorted(out, key=lambda e: (_endpoint_order(e[0]), e[1]))


def render_differs(runs: list[LaneRun]) -> str:
    """Exactly one line when the lanes agree; otherwise the checks, by label."""
    lanes = {run.lane for run in runs} - {None}
    if len(lanes) < 2:
        return "**Differs by lane:** nothing to compare, fewer than two lanes reported."
    entries = differs_by_lane(runs)
    if not entries:
        return "**Differs by lane:** nothing. Every check that ran on more than one lane had the same result on each."
    lines = [f"**Differs by lane:** {len(entries)} check(s).", ""]
    for endpoint, label, by_lane in entries[:DIFFERS_SHOWN]:
        groups: dict[str, list[str]] = {}
        for lane in sorted(by_lane, key=_lane_order):
            groups.setdefault(by_lane[lane], []).append(LANE_TITLES.get(lane, lane))
        verdict = "; ".join(f"{status} on {', '.join(names)}" for status, names in groups.items())
        lines.append(f"- {endpoint} · `{label}`: {verdict}")
    if len(entries) > DIFFERS_SHOWN:
        lines.append(f"- … and {len(entries) - DIFFERS_SHOWN} more in the functional reports.")
    return "\n".join(lines)


def render_eval_revision(reports: list[Report]) -> str:
    """The one line saying which MCP revision the LLM eval negotiated.

    The functional suite runs on several revisions; the eval runs on one, and
    without this line the table above it invites reading its numbers as
    covering them all.
    """
    if not reports:
        return ""
    revisions = sorted({r.get("protocol_version", "") for r in reports} - {""})
    if not revisions:
        return "LLM eval revision: not recorded in its reports.\n"
    noun = "revision" if len(revisions) == 1 else "revisions"
    return f"LLM eval ran at {noun} {', '.join(revisions)}.\n"
