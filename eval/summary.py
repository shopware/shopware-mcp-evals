#!/usr/bin/env python3
"""Render the GitHub Actions job summary for a whole eval run.

The workflow runs `eval/runner.py` three times — admin primary, admin second
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
    python -m eval.summary --rows results/rows --comparison results/eval-comparison.json
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path

from eval.compare_runs import render_actionable, render_detail, render_split, render_unmatched
from ownership import TIER_ORDER

# Prefix for synthetic fixture ids, used only when a summary row predates the
# `ids` field. Chosen because a real fixture id is a YAML identifier and can
# never start with it, so `startswith` is a safe test for "not a real fixture".
ANON = "?"


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
    # Counted per fixture, not per fixture-and-run. The admin primary and the
    # second validator grade the same 90 admin fixtures, so summing `total`
    # across rows doubled every admin denominator — dev-tools read 36/42 for 21
    # fixtures — and a fixture both models missed was listed twice in the
    # failing cell. Worse, that weighting is backwards: a both-model failure
    # counted twice against the rate while a single-model one counted once,
    # inflating exactly the capability-gap misses the cross-model table exists
    # to discount.
    graded: dict[str, collections.Counter] = {}
    failed: dict[str, collections.Counter] = {}
    advisory: dict[str, bool] = {}
    for index, r in enumerate(rows):
        for tier, b in (r.get("by_tier") or {}).items():
            ids = b.get("ids")
            fails = b.get("failed_ids") or []
            if ids is None:
                # A row written before `ids` existed, so nothing can dedupe: fall
                # back to the old summed behaviour by giving every slot a
                # row-unique placeholder. `passed`/`total` drive the split rather
                # than `failed_ids`, which older rows could under-populate — using
                # the id list alone would report such a row as entirely clean.
                total, fails = b.get("total", 0), list(fails)
                anonymous = max(0, (total - b.get("passed", 0)) - len(fails))
                fails += [f"{ANON}fail·{tier}#{index}#{n}" for n in range(anonymous)]
                ids = fails + [f"{ANON}pass·{tier}#{index}#{n}" for n in range(max(0, total - len(fails)))]
            graded.setdefault(tier, collections.Counter()).update(ids)
            failed.setdefault(tier, collections.Counter()).update(fails)
            # Advisory only if every suite that contributed to this tier was.
            advisory[tier] = advisory.get(tier, True) and bool(r.get("advisory"))
    if not graded:
        return ""

    ordered = [t for t in TIER_ORDER if t in graded] + [t for t in graded if t not in TIER_ORDER]
    lines = [
        "### By owner",
        "",
        "Failures are not worth the same: core ships to every merchant, the plugins",
        "are optional. Core is held to its own denominator so a regression there",
        "cannot be averaged away by clean plugin numbers.",
        "",
        "Counted per fixture across every suite, so a fixture graded by both admin",
        "runs counts once rather than twice. **Clean** is fixtures that passed on every",
        "model that graded them, which is why this rate is stricter than the per-suite",
        "rates above — one model missing once is enough to leave a fixture out of it.",
        "",
        "**Bold** in the last column marks a fixture that at least two models graded",
        "and *all* of them failed — the actionable set, matching the both-fail row of",
        "the cross-model table below. Plain means either some model passed it (usually",
        "the weaker one's capability gap) or only one model graded it at all, and a",
        "single run is not evidence about a description.",
        "",
        "| Owner | Clean | Rate | Failed on every model | Enforcement | Failing fixtures |",
        "|---|---:|---:|---:|---|---|",
    ]
    for tier in ordered:
        runs_by_id, fails_by_id = graded[tier], failed[tier]
        total = len(runs_by_id)
        # A fixture counts as failing the owner if any run missed it; the
        # failed-everywhere subset is the one worth acting on.
        #
        # The two-run floor is the point. The Store suite runs a single model, so
        # without it every store failure satisfied "failed on every model that
        # graded it" and got bolded as actionable — two of them were, while the
        # cross-model table right below reported one. That is precisely the
        # single-run noise the two-model split exists to discount, presented with
        # the weight of a confirmed finding.
        #
        # Placeholders from a legacy row are excluded too: they count towards the
        # denominator but have no id to correlate across runs.
        failing_ids = sorted(fails_by_id)
        consistent = {
            i
            for i in failing_ids
            if not i.startswith(ANON) and runs_by_id.get(i, 0) >= 2 and fails_by_id[i] >= runs_by_id[i]
        }
        passed = total - len(failing_ids)
        pct = round(100 * passed / total) if total else 0
        lines.append(
            f"| {tier} | {passed}/{total} | {pct}% | {len(consistent) or '—'} | "
            f"{_enforcement(tier, advisory[tier])} | {_failing(failing_ids, consistent)} |"
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


def _failing(ids: list, consistent: set | None = None) -> str:
    """Failing fixture ids, the all-models ones bolded.

    Truncation puts the bolded ids first: with a cap of six, an owner with eight
    failures could otherwise hide its only actionable one behind five
    capability-gap misses.

    Placeholder ids from a legacy row have no fixture name to print, so they are
    reported as a count rather than rendered — printing `?fail·core#0#2` would
    read as a fixture that does not exist.
    """
    named = sorted(i for i in ids if not i.startswith(ANON))
    anonymous = len(ids) - len(named)
    if not named:
        return f"{anonymous} unnamed (older run)" if anonymous else "—"

    consistent = consistent or set()
    ordered = sorted(named, key=lambda i: (i not in consistent, i))
    shown = ", ".join(f"**`{i}`**" if i in consistent else f"`{i}`" for i in ordered[:6])
    extra = []
    if len(ordered) > 6:
        extra.append(f"+{len(ordered) - 6} more")
    if anonymous:
        extra.append(f"+{anonymous} unnamed")
    return shown + (f" ({', '.join(extra)})" if extra else "")


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
            render_detail(cmp_, primary, second),
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
