#!/usr/bin/env python3
"""Did this run get more expensive than the last one, and by enough to care?

A cost regression is invisible in a pass rate. Discovery that starts taking two
extra search rounds per fixture, or a tool that begins returning ten times the
payload, moves the bill and nothing else — the gate stays green while every
client of this server quietly pays more.

Compared per fixture, never in total: fixture counts change (six negatives
landed in one commit), and a total that rose because the suite grew is not a
regression. Tokens are the headline rather than dollars, because a price-table
edit would otherwise read as a regression in the thing being measured.

**This never gates.** Provider-side changes to caching or tokenization move
these numbers through no fault of the server, and a red build for that is one
people learn to bypass. It warns.

Usage:
    python -m eval.cost_drift --current results/eval-primary.json \\
                              --previous results/eval-primary-lastnight.json
"""

import argparse
import json
import sys
from pathlib import Path

# Below this, a change is noise: model nondeterminism alone moves per-fixture
# token counts by a few percent between runs on an identical suite.
DEFAULT_THRESHOLD = 0.25

# What is worth watching, and what each one means when it moves.
TRACKED = (
    ("input_tokens_per_fixture", "input tokens per fixture", "the model is being sent more context"),
    ("output_tokens_per_fixture", "output tokens per fixture", "the model is writing more"),
    ("payload_bytes_p50", "median tool-result payload", "a tool is returning more data"),
    ("surface_tokens_peak", "peak advertised surface", "discovery is pulling in more tools"),
)


def metrics(report: dict) -> dict:
    """The comparable per-fixture figures from one report.

    Per fixture, so a suite that grew does not read as a regression. Returns
    only what the report actually carries: a report written before a field
    existed must produce no comparison rather than a fake one against zero.
    """
    cost = report.get("cost") or {}
    graded = cost.get("graded") or 0
    tokens = cost.get("tokens") or {}
    out = {}
    if graded:
        # Cached and full-price input together: what moved is how much context
        # the model was handed, regardless of what it was billed for.
        total_input = (tokens.get("input") or 0) + (tokens.get("cached_input") or 0)
        out["input_tokens_per_fixture"] = total_input / graded
        out["output_tokens_per_fixture"] = (tokens.get("output") or 0) / graded
    for key in ("payload_bytes_p50", "surface_tokens_peak"):
        if cost.get(key) is not None:
            out[key] = cost[key]
    return out


def compare(current: dict, previous: dict, threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """Metrics that moved by more than `threshold`, in either direction.

    Improvements are reported too. A sudden halving is as much a signal that
    something changed as a doubling, and is the shape a silently-broken run
    takes — fewer steps because discovery stopped happening at all.
    """
    now, before = metrics(current), metrics(previous)
    findings = []
    for key, label, meaning in TRACKED:
        if key not in now or key not in before or not before[key]:
            continue
        change = (now[key] - before[key]) / before[key]
        if abs(change) >= threshold:
            findings.append(
                {
                    "metric": key,
                    "label": label,
                    "meaning": meaning,
                    "previous": before[key],
                    "current": now[key],
                    "change": change,
                }
            )
    return sorted(findings, key=lambda f: -abs(f["change"]))


def render(findings: list[dict], threshold: float = DEFAULT_THRESHOLD) -> str:
    if not findings:
        return f"Cost per fixture is within {threshold:.0%} of the previous run.\n"
    lines = [
        f"### Cost drift (advisory, threshold {threshold:.0%})",
        "",
        "| Metric | Previous | Current | Change | What it usually means |",
        "|---|---:|---:|---:|---|",
    ]
    for f in findings:
        arrow = "▲" if f["change"] > 0 else "▼"
        lines.append(
            f"| {f['label']} | {f['previous']:,.0f} | {f['current']:,.0f} | "
            f"{arrow} {abs(f['change']):.0%} | {f['meaning']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _load(path: str) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except OSError, json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--current", required=True, help="This run's report")
    parser.add_argument("--previous", help="The report to compare against; omitted means no comparison")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Relative change worth reporting")
    args = parser.parse_args()

    current = _load(args.current)
    if current is None:
        print(f"::warning::Cost drift skipped: could not read {args.current}", file=sys.stderr)
        return 0

    previous = _load(args.previous) if args.previous else None
    if previous is None:
        # The ordinary case on a first run or a renamed report. Not a warning:
        # there is nothing wrong, there is just nothing to compare against.
        print("No previous report to compare against; cost drift skipped.")
        return 0

    findings = compare(current, previous, args.threshold)
    print(render(findings, args.threshold))
    for f in findings:
        if f["change"] > 0:
            print(f"::warning::{f['label']} is up {f['change']:.0%} per fixture — {f['meaning']}", file=sys.stderr)
    # Always 0: see the module docstring. This warns, it does not gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
