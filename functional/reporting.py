"""Reusable pass/fail/skip reporting harness for the functional runners.

Replaces the copy-pasted color palette, counters, log_* helpers, and the
results-file writer that both bash runners duplicated. One Reporter instance
tracks a run, prints colored progress, and writes the machine-readable report
consumed as a CI artifact (results/functional-*.json).

Record shapes match the historical bash runner's output exactly:
  structural check : {tool: "check", label, status: "pass"|"fail"[, error]}
  tool assertion   : {tool, label, status: "pass", preview} | {..., "fail", error}
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.result_schema import FunctionalRecord, FunctionalReport, ToolHealth


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

    def __init__(self, server: str, *, color: bool | None = None):
        self.server: str = server
        self.passed: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.records: list[FunctionalRecord] = []
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

    def tool_skip(self, tool: str, label: str, reason: str) -> None:
        """A tool that did not run, and why.

        Recorded rather than merely counted, because the eval gate downstream has
        to tell three states apart: a tool proven to work, one proven broken, and
        one nobody tried. A skip that leaves no record reads as the first, which
        is how a suite starts overstating its own coverage.
        """
        print(f"  {self.c.YELLOW}SKIP{self.c.RESET} {label}: {reason}")
        self.skipped += 1
        self.records.append(FunctionalRecord(tool=tool, label=label, status="skipped", reason=reason))

    # -- internals ----------------------------------------------------------
    def _pass(self, label: str, record: FunctionalRecord) -> None:
        print(f"  {self.c.GREEN}PASS{self.c.RESET} {label}")
        self.passed += 1
        self.records.append(record)

    def _fail(self, label: str, error: str, record: FunctionalRecord) -> None:
        print(f"  {self.c.RED}FAIL{self.c.RESET} {label}: {error}")
        self.failed += 1
        self.records.append(record)

    # -- finalization -------------------------------------------------------
    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0

    def summary(self) -> None:
        c = self.c
        print(
            f"\n{c.BOLD}Results: {c.GREEN}{self.passed} passed{c.RESET}, "
            f"{c.RED}{self.failed} failed{c.RESET}, "
            f"{c.YELLOW}{self.skipped} skipped{c.RESET} / {self.total} total"
        )

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
        report = FunctionalReport(
            timestamp=datetime.now(UTC).isoformat(),
            server=self.server,
            total=self.total,
            tools=self.records,
            health=self.tool_health(),
            **{"pass": self.passed, "fail": self.failed, "skip": self.skipped},
        )
        path.write_text(json.dumps(report, indent=2))
        print(f"Report: {path}")

    def write_health(self, path: Path) -> None:
        """The health map on its own, for the eval job to consume as an artifact."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.tool_health(), indent=2, sort_keys=True))
        print(f"Tool health: {path}")
