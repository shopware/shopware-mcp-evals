"""Reusable pass/fail/skip reporting harness for the functional runners.

Replaces the copy-pasted color palette, counters, log_* helpers, and the
results-file writer that both bash runners duplicated. One Reporter instance
tracks a run, prints colored progress, and writes the machine-readable report
consumed as a CI artifact (results/functional-*.json).

Record shapes match the historical bash runner's output exactly:
  structural check : {tool: "check", label, status: "pass"|"fail"[, error]}
  tool assertion   : {tool, label, status: "pass", preview} | {..., "fail", error}

A failure functional/known_failures.py explains also carries `known_issue`. It is
shown like any other failure but does not count towards the exit code, and in CI
`annotations()` reports it as expected, naming the issue it is waiting for.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.result_schema import FunctionalRecord, FunctionalReport, ToolHealth
from functional.known_failures import KNOWN_FAILURES, KnownFailure

# Enough FAIL lines to name a pattern, few enough that the annotation stays
# readable. The report JSON has every one.
_ANNOTATION_LINES = 8
_ANNOTATION_ERROR_CHARS = 300


class _Palette:
    """ANSI colors, auto-disabled when output is not a TTY or NO_COLOR is set."""

    def __init__(self, enabled: bool):
        self.RED: str = "\033[0;31m" if enabled else ""
        self.GREEN: str = "\033[0;32m" if enabled else ""
        self.YELLOW: str = "\033[1;33m" if enabled else ""
        self.BOLD: str = "\033[1m" if enabled else ""
        self.RESET: str = "\033[0m" if enabled else ""


class Reporter:
    """Collects pass/fail/skip results for one functional run."""

    def __init__(
        self,
        server: str,
        *,
        color: bool | None = None,
        protocol_version: str | None = None,
        known: tuple[KnownFailure, ...] = KNOWN_FAILURES,
    ):
        self.server: str = server
        self.protocol_version: str | None = protocol_version
        self.known: tuple[KnownFailure, ...] = known
        self.passed: int = 0
        self.failed: int = 0
        self.expected_failed: int = 0
        self.skipped: int = 0
        self.records: list[FunctionalRecord] = []
        # Every skip as printed, bare ones included, which are not recorded.
        self.skip_lines: list[str] = []
        # Known failures whose check passed this run: probably fixed.
        self.recovered: list[tuple[str, KnownFailure]] = []
        enabled = (sys.stdout.isatty() and "NO_COLOR" not in os.environ) if color is None else color
        self.c: _Palette = _Palette(enabled)

    # -- output helpers -----------------------------------------------------
    def banner(self, title: str) -> None:
        print(f"{self.c.BOLD}{title}{self.c.RESET}")

    def section(self, title: str) -> None:
        print(f"\n{self.c.BOLD}{title}{self.c.RESET}")

    def info(self, msg: str) -> None:
        print(msg)

    # -- structural checks (tool == "check") --------------------------------
    def check_pass(self, label: str) -> None:
        self._pass(label, FunctionalRecord(tool="check", label=label, status="pass"))

    def check_fail(self, label: str, error: str) -> None:
        self._fail(label, error, FunctionalRecord(tool="check", label=label, status="fail", error=error))

    # -- tool assertions ----------------------------------------------------
    def tool_pass(self, tool: str, label: str, preview: str = "") -> None:
        self._pass(label, FunctionalRecord(tool=tool, label=label, status="pass", preview=preview))

    def tool_fail(self, tool: str, label: str, error: str) -> None:
        self._fail(label, error, FunctionalRecord(tool=tool, label=label, status="fail", error=error))

    # -- skips --------------------------------------------------------------
    def skip(self, label: str) -> None:
        """A structural check that did not run. Counted, not attributed."""
        print(f"  {self.c.YELLOW}SKIP{self.c.RESET} {label}")
        self.skipped += 1
        self.skip_lines.append(label)

    def check_skip(self, label: str, reason: str) -> None:
        """A structural check that did not run, recorded with why.

        For a skip someone has to act on later — a check waiting for a server
        change — where a bare count would let it be forgotten. Still `check`, so
        it contributes nothing to the tool health map.
        """
        print(f"  {self.c.YELLOW}SKIP{self.c.RESET} {label}: {reason}")
        self.skipped += 1
        self.skip_lines.append(f"{label}: {reason}")
        self.records.append(FunctionalRecord(tool="check", label=label, status="skipped", reason=reason))

    def tool_skip(self, tool: str, label: str, reason: str) -> None:
        """A tool that did not run, and why.

        Recorded rather than merely counted, because the eval gate downstream has
        to tell three states apart: a tool proven to work, one proven broken, and
        one nobody tried. A skip that leaves no record reads as the first, which
        is how a suite starts overstating its own coverage.
        """
        print(f"  {self.c.YELLOW}SKIP{self.c.RESET} {label}: {reason}")
        self.skipped += 1
        self.skip_lines.append(f"{label}: {reason}")
        self.records.append(FunctionalRecord(tool=tool, label=label, status="skipped", reason=reason))

    # -- internals ----------------------------------------------------------
    def _pass(self, label: str, record: FunctionalRecord) -> None:
        print(f"  {self.c.GREEN}PASS{self.c.RESET} {label}")
        self.passed += 1
        self.records.append(record)
        if entry := next((k for k in self.known if k.matches_label(label)), None):
            self.recovered.append((label, entry))

    def _fail(self, label: str, error: str, record: FunctionalRecord) -> None:
        self.failed += 1
        if entry := next((k for k in self.known if k.matches(label, error)), None):
            print(f"  {self.c.YELLOW}FAIL (expected, {entry.issue}){self.c.RESET} {label}: {error}")
            self.expected_failed += 1
            record["known_issue"] = entry.issue
        else:
            print(f"  {self.c.RED}FAIL{self.c.RESET} {label}: {error}")
        self.records.append(record)

    # -- finalization -------------------------------------------------------
    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def unexpected_failed(self) -> int:
        return self.failed - self.expected_failed

    @property
    def exit_code(self) -> int:
        """Non-zero for a failure nobody has explained yet.

        A known failure is not news, so it does not fail the step. It is still
        counted, printed and annotated as expected.
        """
        return 1 if self.unexpected_failed else 0

    def tally(self) -> str:
        expected = f" ({self.expected_failed} expected)" if self.expected_failed else ""
        return (
            f"Results: {self.passed} passed, {self.failed} failed{expected}, "
            f"{self.skipped} skipped / {self.total} total"
        )

    def summary(self) -> None:
        c = self.c
        expected = f" ({self.expected_failed} expected)" if self.expected_failed else ""
        print(
            f"\n{c.BOLD}Results: {c.GREEN}{self.passed} passed{c.RESET}, "
            f"{c.RED}{self.failed} failed{expected}{c.RESET}, "
            f"{c.YELLOW}{self.skipped} skipped{c.RESET} / {self.total} total"
        )
        # The annotations only reach GitHub, so a local run says it here too.
        for label, entry in self.recovered:
            print(
                f"{c.YELLOW}Known failure passed: {label}. Was waiting on {entry.issue}: "
                f"if it is fixed, delete the entry in functional/known_failures.py.{c.RESET}"
            )

    def annotations(self, title: str, *, advisory: bool = False, skips: bool = False) -> list[str]:
        """GitHub workflow commands that say what this run means, not just its exit code.

        At most four, each answering one question a reader of the run has:

        - an unexpected failure is an `error`, or a `warning` on an `advisory`
          suite whose step stays green. It says the failure is new or unfiled.
        - a known failure is a `notice` naming its issue and what it waits for.
        - a known check that passed is a `warning`: the issue may be fixed, and
          the entry should go.
        - with `skips`, a `notice` with the tally and every skip, so a skipped
          check cannot pass for coverage.

        Built from the records rather than from the log, so there is no ANSI or
        line format to parse.
        """
        out: list[str] = []
        unexpected = [r for r in self.records if r["status"] == "fail" and "known_issue" not in r]
        expected = [r for r in self.records if r["status"] == "fail" and "known_issue" in r]

        if unexpected:
            consequence = (
                "This suite is advisory, so the job stays green: read the failure before trusting the run."
                if advisory
                else "The job fails on it."
            )
            out.append(
                workflow_command(
                    "warning" if advisory else "error",
                    title,
                    [
                        f"{len(unexpected)} unexpected failure(s): not in functional/known_failures.py, "
                        f"so new or not filed yet. {consequence} Fix it, or file it and add an entry there.",
                        self.tally(),
                        *_fail_lines(unexpected),
                    ],
                )
            )

        if expected:
            lines = [
                f"{len(expected)} expected failure(s), already filed and waiting for a fix. They do not fail the job.",
                self.tally(),
            ]
            seen: set[str] = set()
            for record in expected[:_ANNOTATION_LINES]:
                lines.append(_fail_line(record))
                entry = next((k for k in self.known if k.issue == record.get("known_issue")), None)
                if entry and entry.issue not in seen:
                    seen.add(entry.issue)
                    lines.append(entry.describe())
            out.append(workflow_command("notice", title, lines))

        if self.recovered:
            lines = [
                f"{len(self.recovered)} known failure(s) passed this run. Check whether the issue is fixed, "
                "then delete the entry in functional/known_failures.py so a regression is reported as new."
            ]
            lines += [f"PASS {label}. Was waiting on {entry.issue} ({entry.url})." for label, entry in self.recovered]
            out.append(workflow_command("warning", title, lines))

        if skips:
            shown = [f"SKIP {line[:_ANNOTATION_ERROR_CHARS]}" for line in self.skip_lines[:_ANNOTATION_LINES]]
            more = len(self.skip_lines) - len(shown)
            if more > 0:
                shown.append(f"... and {more} more in the report JSON.")
            out.append(workflow_command("notice", title, [self.tally(), *(shown or ["No skipped checks."])]))
        return out

    def tool_health(self) -> dict[str, ToolHealth]:
        """Per-tool verdict, for the gate that decides where LLM budget goes.

        One entry per tool, worst status wins: a tool asserted several times is
        only healthy if every assertion held. `fail` beats `skipped` beats `pass`,
        because the question the gate asks is "is there any reason not to trust
        this tool", not "did it ever work once".

        Structural checks (`tool == "check"`) are excluded — they describe the
        server, not a tool, and have no fixtures to gate.
        """
        rank = {"pass": 0, "skipped": 1, "fail": 2}
        health: dict[str, ToolHealth] = {}
        for record in self.records:
            tool = record.get("tool", "")
            if not tool or tool == "check":
                continue
            status = record.get("status", "fail")
            current = health.get(tool)
            if current is None or rank[status] > rank[current["status"]]:
                entry = ToolHealth(status=status)
                if reason := record.get("error") or record.get("reason"):
                    entry["reason"] = reason
                health[tool] = entry
        return health

    def write_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A literal, not the keyword form: `pass` is a keyword, and splatting it in
        # from a dict of ints stops type-checking once the record has a str field.
        report: FunctionalReport = {
            "timestamp": datetime.now(UTC).isoformat(),
            "server": self.server,
            "pass": self.passed,
            "fail": self.failed,
            "skip": self.skipped,
            "total": self.total,
            "tools": self.records,
            "health": self.tool_health(),
        }
        if self.expected_failed:
            report["expected_fail"] = self.expected_failed
        if self.protocol_version:
            report["protocol_version"] = self.protocol_version
        path.write_text(json.dumps(report, indent=2))
        print(f"Report: {path}")

    def write_health(self, path: Path) -> None:
        """The health map on its own, for the eval job to consume as an artifact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.tool_health(), indent=2, sort_keys=True))
        print(f"Tool health: {path}")


def _escape_data(text: str) -> str:
    """A workflow command's message, escaped the way the runner unescapes it."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def workflow_command(level: str, title: str, lines: list[str]) -> str:
    return f"::{level} title={_escape_property(title)}::{_escape_data(chr(10).join(lines))}"


def _fail_line(record: FunctionalRecord) -> str:
    error = record.get("error", "")
    if len(error) > _ANNOTATION_ERROR_CHARS:
        error = error[:_ANNOTATION_ERROR_CHARS] + "..."
    return f"FAIL {record['label']}: {error}"


def _fail_lines(records: list[FunctionalRecord]) -> list[str]:
    lines = [_fail_line(record) for record in records[:_ANNOTATION_LINES]]
    if len(records) > _ANNOTATION_LINES:
        lines.append(f"... and {len(records) - _ANNOTATION_LINES} more in the report JSON.")
    return lines
