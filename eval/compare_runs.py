#!/usr/bin/env python3
"""Compare two eval reports (primary model vs second validator).

The premise of running two models is that the *intersection* carries the signal:

  both fail    -> the tool description is the problem. Actionable here.
  only weak    -> a capability gap in the weaker model, not a description bug.
  only strong  -> noise, almost always a flaky discovery run.
  both pass    -> nothing to do.

A single model's pass rate cannot make that distinction, which is why the
second validator exists. This script reduces two reports to that split and
emits the both-fail set grouped by tool — a tool failing on all 3 of its
prompts is a description to rewrite; failing on 1 of 3 is usually one awkward
prompt.

Gating is opt-in via --gate so the thresholds can be measured before they are
enforced.

Usage:
    python eval/compare_runs.py results/eval-primary.json results/eval-second.json
    python eval/compare_runs.py a.json b.json --gate both --min-pass-rate 0.9
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path


def discovery_index(report: dict) -> dict[str, dict]:
    """Map fixture id -> result record for graded discovery results.

    Skipped fixtures (expected tool not registered on the instance) are dropped:
    they never gate, so counting them would dilute both rates.
    """
    mode = report.get("modes", {}).get("discovery")
    if not mode:
        return {}
    return {r["id"]: r for r in mode.get("results", []) if not r.get("skipped")}


def pass_rate(index: dict[str, dict]) -> tuple[int, int, float]:
    total = len(index)
    passed = sum(1 for r in index.values() if r.get("passed"))
    return passed, total, (passed / total if total else 0.0)


def compare(primary: dict, second: dict) -> dict:
    """Split the shared fixture set four ways by which models passed."""
    a, b = discovery_index(primary), discovery_index(second)
    shared = sorted(set(a) & set(b))

    buckets = collections.defaultdict(list)
    for fid in shared:
        key = ("pass" if a[fid].get("passed") else "fail", "pass" if b[fid].get("passed") else "fail")
        buckets[key].append(fid)

    both_fail = buckets[("fail", "fail")]
    # Group the actionable set by the tool whose description is implicated.
    by_tool = collections.defaultdict(list)
    for fid in both_fail:
        by_tool[a[fid].get("expected_tool", "?")].append(fid)

    a_passed, a_total, a_rate = pass_rate(a)
    b_passed, b_total, b_rate = pass_rate(b)

    return {
        "primary": {
            "model": primary.get("model", "?"),
            "passed": a_passed,
            "total": a_total,
            "rate": a_rate,
        },
        "second": {
            "model": second.get("model", "?"),
            "passed": b_passed,
            "total": b_total,
            "rate": b_rate,
        },
        "shared": len(shared),
        "both_pass": buckets[("pass", "pass")],
        "only_primary": buckets[("pass", "fail")],
        "only_second": buckets[("fail", "pass")],
        "both_fail": both_fail,
        "both_fail_by_tool": {t: sorted(ids) for t, ids in sorted(by_tool.items())},
        # Fixtures present in one report but not the other — a mismatch means the
        # two runs used different fixture files or one crashed mid-sweep.
        "unmatched": sorted(set(a) ^ set(b)),
    }


def _verdict(rate: float, threshold: float) -> str:
    return "PASS" if rate >= threshold else "FAIL"


def render(cmp_: dict, threshold: float) -> str:
    p, s = cmp_["primary"], cmp_["second"]
    lines = [
        "### Cross-model comparison (discovery mode)",
        "",
        "| Model | Passed | Rate | vs threshold |",
        "|---|---|---|---|",
    ]
    for role, m in (("primary", p), ("second", s)):
        pct = round(100 * m["rate"])
        lines.append(
            f"| `{m['model']}` ({role}) | {m['passed']}/{m['total']} | {pct}% | "
            f"{_verdict(m['rate'], threshold)} (>= {round(100 * threshold)}%) |"
        )

    lines += [
        "",
        "| Outcome | Count | Meaning |",
        "|---|---|---|",
        f"| both pass | {len(cmp_['both_pass'])} | nothing to do |",
        f"| only primary | {len(cmp_['only_primary'])} | weaker model's capability gap |",
        f"| only second | {len(cmp_['only_second'])} | noise / flaky discovery |",
        f"| **both fail** | **{len(cmp_['both_fail'])}** | **description problem — actionable** |",
        "",
    ]

    if cmp_["both_fail_by_tool"]:
        lines += [
            "#### Descriptions to fix (both models failed)",
            "",
            "| Tool | Failing prompts |",
            "|---|---|",
        ]
        # Most-failed tool first: 3/3 is a description rewrite, 1/3 an awkward prompt.
        for tool, ids in sorted(cmp_["both_fail_by_tool"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"| `{tool}` | {len(ids)} — {', '.join(f'`{i}`' for i in ids)} |")
        lines.append("")
    else:
        lines += ["No fixture failed for both models.", ""]

    if cmp_["unmatched"]:
        lines += [
            f"> Warning: {len(cmp_['unmatched'])} fixture(s) graded in only one run "
            f"({', '.join(cmp_['unmatched'][:5])}...). The runs are not comparable.",
            "",
        ]

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("primary", help="JSON report from the primary model")
    parser.add_argument("second", help="JSON report from the second validator")
    parser.add_argument("--min-pass-rate", type=float, default=0.9)
    parser.add_argument(
        "--gate",
        choices=("none", "primary", "both"),
        default="none",
        help="which rates must clear --min-pass-rate for a zero exit (default: none, report only)",
    )
    parser.add_argument("--output", help="write the comparison JSON here")
    args = parser.parse_args()

    try:
        primary = json.loads(Path(args.primary).read_text())
        second = json.loads(Path(args.second).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read both reports: {exc}", file=sys.stderr)
        return 2

    cmp_ = compare(primary, second)
    markdown = render(cmp_, args.min_pass_rate)
    print(markdown)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(markdown + "\n")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(cmp_, indent=2))

    if args.gate == "none":
        return 0
    rates = [cmp_["primary"]["rate"]]
    if args.gate == "both":
        rates.append(cmp_["second"]["rate"])
    return 0 if all(r >= args.min_pass_rate for r in rates) else 1


if __name__ == "__main__":
    sys.exit(main())
