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


class _Palette:
    """ANSI colors, auto-disabled when output is not a TTY or NO_COLOR is set."""

    def __init__(self, enabled: bool):
        self.RED = "\033[0;31m" if enabled else ""
        self.GREEN = "\033[0;32m" if enabled else ""
        self.YELLOW = "\033[1;33m" if enabled else ""
        self.BOLD = "\033[1m" if enabled else ""
        self.RESET = "\033[0m" if enabled else ""


class Reporter:
    """Collects pass/fail/skip results for one functional run."""

    def __init__(self, server: str, *, color: bool | None = None):
        self.server = server
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.records: list[dict] = []
        enabled = (
            (sys.stdout.isatty() and "NO_COLOR" not in os.environ)
            if color is None
            else color
        )
        self.c = _Palette(enabled)

    # -- output helpers -----------------------------------------------------
    def banner(self, title: str) -> None:
        print(f"{self.c.BOLD}{title}{self.c.RESET}")

    def section(self, title: str) -> None:
        print(f"\n{self.c.BOLD}{title}{self.c.RESET}")

    def info(self, msg: str) -> None:
        print(msg)

    # -- structural checks (tool == "check") --------------------------------
    def check_pass(self, label: str) -> None:
        self._pass(label, {"tool": "check", "label": label, "status": "pass"})

    def check_fail(self, label: str, error: str) -> None:
        self._fail(
            label, error,
            {"tool": "check", "label": label, "status": "fail", "error": error},
        )

    # -- tool assertions ----------------------------------------------------
    def tool_pass(self, tool: str, label: str, preview: str = "") -> None:
        self._pass(
            label,
            {"tool": tool, "label": label, "status": "pass", "preview": preview},
        )

    def tool_fail(self, tool: str, label: str, error: str) -> None:
        self._fail(
            label, error,
            {"tool": tool, "label": label, "status": "fail", "error": error},
        )

    # -- skips (counted, not recorded, matching the bash runner) ------------
    def skip(self, label: str) -> None:
        print(f"  {self.c.YELLOW}SKIP{self.c.RESET} {label}")
        self.skipped += 1

    # -- internals ----------------------------------------------------------
    def _pass(self, label: str, record: dict) -> None:
        print(f"  {self.c.GREEN}PASS{self.c.RESET} {label}")
        self.passed += 1
        self.records.append(record)

    def _fail(self, label: str, error: str, record: dict) -> None:
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

    def write_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "timestamp": datetime.now(UTC).isoformat(),
            "server": self.server,
            "pass": self.passed,
            "fail": self.failed,
            "skip": self.skipped,
            "total": self.total,
            "tools": self.records,
        }
        path.write_text(json.dumps(report, indent=2))
        print(f"Report: {path}")
