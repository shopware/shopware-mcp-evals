#!/usr/bin/env python3
"""Render the GitHub Actions job summary for a whole eval run.

The workflow runs `eval/run.py` three times — admin primary, admin second
validator, Store/UCP advisory — in three separate processes, with the
cross-model comparison step in between. Each one used to append its own
markdown table to GITHUB_STEP_SUMMARY, which produced:

  * three one-row tables instead of one three-row table, each re-printing the
    same header, because markdown appended from separate processes cannot be
    joined into a single table;
  * the primary and second-validator rates printed twice — once in their own
    table, again in the cross-model table underneath;
  * an unlabelled third row (`openai` / `gpt-4o`, 42 fixtures) that nothing
    identified as the Store suite.

So the runs now emit their verdicts as JSON rows (`run.py --summary-row`) and
this module renders everything once, at the end of the job. Ordering comes
from the row filenames, which the workflow numbers.

Usage:
    python eval/summary.py --rows results/rows --comparison results/eval-comparison.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ownership.py is at the repo root; compare_runs.py is the sibling. The root
# goes first, but eval/ is *appended* rather than inserted: eval/run.py and
# functional/run.py are both called `run`, and putting eval/ ahead of
# functional/ makes `import run` resolve to the wrong one.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parent))

from compare_runs import render_actionable, render_split, render_unmatched  # noqa: E402

from ownership import TIER_ORDER  # noqa: E402


def load_rows(rows_dir: Path) -> list[dict]:
    """Read the per-run rows, ordered by filename.

    A missing directory or an unreadable row is not fatal: the summary is a
    reporting convenience and must never be the thing that turns a run red.
    An absent row is reported by its absence — a run that did not happen has
    no line in the table, which is the honest rendering.
    """
    if not rows_dir.is_dir():
        return []
    rows = []
    for path in sorted(rows_dir.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::warning::Could not read summary row {path}: {exc}", file=sys.stderr)
    return rows


def render_runs(rows: list[dict]) -> str:
    """One table for every eval run in the job, suite-labelled."""
    if not rows:
        return "No eval run reported a result.\n"

    lines = [
        "| Suite | Provider | Model | Pass rate | Graded | Errors | Throttled | Gate |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        gate = r.get("gate", "?")
        # An advisory suite is continue-on-error, so a FAIL there is a finding
        # to read, not a broken build. Saying so in the cell stops it being
        # mistaken for the gating verdict.
        if r.get("advisory"):
            gate = f"{gate} (advisory)"
        lines.append(
            f"| {r.get('suite', '?')} | `{r.get('provider', '?')}` | `{r.get('model', '?')}` | "
            f"{round(100 * r.get('rate', 0))}% | {r.get('graded', 0)} | {r.get('errored', 0)} | "
            f"{r.get('throttled', 0)} | {gate} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_tiers(rows: list[dict]) -> str:
    """Pass rates per owning repository, across every suite in the job.

    The run table above says "admin · primary 92%", but admin spans core,
    the dev-tools bundle and the merchant-tools plugin. Those failures are not
    worth the same: core ships to every merchant, the plugins are optional.
    This is the table that says which one moved.
    """
    merged: dict[str, dict] = {}
    for r in rows:
        for tier, b in (r.get("by_tier") or {}).items():
            m = merged.setdefault(tier, {"passed": 0, "total": 0, "failed_ids": [], "advisory": True})
            m["passed"] += b.get("passed", 0)
            m["total"] += b.get("total", 0)
            m["failed_ids"] += b.get("failed_ids") or []
            # Advisory only if every suite that contributed to this tier was.
            m["advisory"] = m["advisory"] and bool(r.get("advisory"))
    if not merged:
        return ""

    ordered = [t for t in TIER_ORDER if t in merged] + [t for t in merged if t not in TIER_ORDER]
    lines = [
        "### By owner",
        "",
        "Failures are not worth the same: core ships to every merchant, the plugins",
        "are optional. Core is held to its own denominator so a regression there",
        "cannot be averaged away by clean plugin numbers.",
        "",
        "| Owner | Passed | Rate | Enforcement | Failing fixtures |",
        "|---|---|---:|---|---|",
    ]
    for tier in ordered:
        m = merged[tier]
        pct = round(100 * m["passed"] / m["total"]) if m["total"] else 0
        lines.append(
            f"| {tier} | {m['passed']}/{m['total']} | {pct}% | {_enforcement(tier, m['advisory'])} | "
            f"{_failing(m['failed_ids'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _enforcement(tier: str, advisory: bool) -> str:
    """What actually fails the build for this tier — not what we wish did.

    Three distinct states, and conflating them is how "optional" gets read as
    "unenforced": an optional plugin's fixtures still count towards its suite's
    aggregate rate, unless the whole suite is continue-on-error.
    """
    if advisory:
        return "advisory"
    if tier.startswith("core"):
        return "**core gate** + suite rate"
    return "suite rate"


def _failing(ids: list) -> str:
    if not ids:
        return "—"
    shown = ", ".join(f"`{i}`" for i in ids[:6])
    return shown + (f" (+{len(ids) - 6} more)" if len(ids) > 6 else "")


def render_comparison(cmp_: dict | None) -> str:
    """The cross-model section, minus the per-model rates.

    Those rates are already in the run table above — repeating them is exactly
    the redundancy this module exists to remove. What is kept is the part the
    run table cannot show: which fixtures both models missed, and what each of
    them picked instead.
    """
    if cmp_ is None:
        return (
            "### Cross-model comparison\n\n"
            "Not available — the comparison step did not produce a report "
            "(one of the two eval runs did not finish).\n"
        )

    primary = cmp_.get("primary", {}).get("model", "primary")
    second = cmp_.get("second", {}).get("model", "second")
    return "\n".join(
        [
            "### Cross-model comparison (discovery mode)",
            "",
            "A fixture both models miss points at the tool description. One only the",
            "weaker model misses is its capability gap; one only the stronger misses is",
            "usually flaky discovery.",
            "",
            render_split(cmp_),
            render_actionable(cmp_, primary, second),
            render_unmatched(cmp_),
        ]
    )


def render(rows: list[dict], cmp_: dict | None) -> str:
    return "\n".join(
        [
            "## MCP evals",
            "",
            render_runs(rows),
            render_tiers(rows),
            render_comparison(cmp_),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--rows", default="results/rows", help="Directory of per-run summary rows (default results/rows)"
    )
    parser.add_argument("--comparison", help="Path to the cross-model comparison JSON (eval/compare_runs.py --output)")
    args = parser.parse_args()

    cmp_ = None
    if args.comparison:
        try:
            cmp_ = json.loads(Path(args.comparison).read_text())
        except OSError, json.JSONDecodeError:
            # Expected whenever the comparison step skipped, which it does when
            # either eval run failed to produce a report. Rendered as a note in
            # the summary rather than warned about here.
            cmp_ = None

    markdown = render(load_rows(Path(args.rows)), cmp_)
    print(markdown)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write(markdown + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
