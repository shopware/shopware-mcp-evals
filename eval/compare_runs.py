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
prompt — together with what each model reached for instead, which is the half
of the finding you actually rewrite the description against.

The rendering is split (render_rates / render_split / render_actionable) so
the CI job summary can skip the per-model rate table, which `eval/summary.py`
already prints for every suite in one place.

Gating is opt-in via --gate so the thresholds can be measured before they are
enforced.

Usage:
    python eval/compare_runs.py results/eval-primary.json results/eval-second.json
    python eval/compare_runs.py a.json b.json --gate both --min-pass-rate 0.9
"""

import argparse
import collections
import json
import sys
from pathlib import Path

# ownership.py lives at the repo root, like mcp_client.py. Adding the root is
# safe; adding eval/ is not, because eval/run.py would then shadow
# functional/run.py for anything that does `import run`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ownership import TIER_ORDER, tier_of  # noqa: E402


def discovery_index(report: dict) -> dict[str, dict]:
    """Map fixture id -> result record for graded discovery results.

    Skipped fixtures (expected tool not registered on the instance) are dropped:
    they never gate, so counting them would dilute both rates.
    """
    mode = report.get("modes", {}).get("discovery")
    if not mode:
        return {}
    return {r["id"]: r for r in mode.get("results", []) if not r.get("skipped")}


def executed(index: dict[str, dict]) -> dict[str, dict]:
    """Drop fixtures that errored out before the model chose anything.

    A transport error (the server answering 500, or throttling with 429) is
    missing data, not a wrong answer. Counting it as a failure understates the
    model badly: one local run reported gpt-4o-mini at 53% when 18 of its 21
    "failures" were the Shopware container erroring — the real rate was 89%.
    """
    return {k: v for k, v in index.items() if not v.get("error")}


def pass_rate(index: dict[str, dict]) -> tuple[int, int, float]:
    """Pass rate over fixtures that actually ran."""
    ran = executed(index)
    total = len(ran)
    passed = sum(1 for r in ran.values() if r.get("passed"))
    return passed, total, (passed / total if total else 0.0)


def compare(primary: dict, second: dict) -> dict:
    """Split the shared fixture set four ways by which models passed."""
    a_all, b_all = discovery_index(primary), discovery_index(second)
    a, b = executed(a_all), executed(b_all)
    # Only fixtures that ran cleanly in BOTH runs can be attributed to a model.
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

    # What each model reached for instead. "Tool X failed" is only half the
    # finding — the other half is what the description was confused with, and
    # that used to be reachable only by downloading the run artifact.
    detail = [
        {
            "id": fid,
            "expected_tool": a[fid].get("expected_tool", "?"),
            "primary_selected": a[fid].get("selected_tool"),
            "second_selected": b[fid].get("selected_tool"),
            "primary_reason": a[fid].get("fail_reason"),
            "second_reason": b[fid].get("fail_reason"),
        }
        for fid in both_fail
    ]

    a_passed, a_total, a_rate = pass_rate(a_all)
    b_passed, b_total, b_rate = pass_rate(b_all)

    return {
        "primary": {
            "model": primary.get("model", "?"),
            "passed": a_passed,
            "total": a_total,
            "rate": a_rate,
            "errored": len(a_all) - len(a),
        },
        "second": {
            "model": second.get("model", "?"),
            "passed": b_passed,
            "total": b_total,
            "rate": b_rate,
            "errored": len(b_all) - len(b),
        },
        "shared": len(shared),
        "both_pass": buckets[("pass", "pass")],
        "only_primary": buckets[("pass", "fail")],
        "only_second": buckets[("fail", "pass")],
        "both_fail": both_fail,
        "both_fail_by_tool": {t: sorted(ids) for t, ids in sorted(by_tool.items())},
        # Same set as both_fail, one record per fixture, carrying what each
        # model picked. Kept alongside the id lists rather than replacing them
        # so existing consumers of the comparison JSON keep working.
        "both_fail_detail": detail,
        # Fixtures present in one report but not the other — a mismatch means the
        # two runs used different fixture files. Compared over the full graded
        # sets, so a fixture that merely errored is reported as errored rather
        # than masquerading as a fixture-file mismatch.
        "unmatched": sorted(set(a_all) ^ set(b_all)),
    }


def _verdict(rate: float, threshold: float) -> str:
    return "PASS" if rate >= threshold else "FAIL"


def render_rates(cmp_: dict, threshold: float) -> str:
    """Per-model pass rates. Used for standalone/local runs.

    The CI job summary does NOT include this: `eval/summary.py` already prints
    one run-overview table covering every suite, and repeating the same two
    rates and verdicts underneath it is what made the old summary read as three
    tables saying the same thing.
    """
    p, s = cmp_["primary"], cmp_["second"]
    lines = [
        "Rates are over fixtures that actually ran. Transport errors (server 500,",
        "throttling 429) are missing data, not wrong answers, and are counted",
        "separately — treating them as failures once understated a model by 36 points.",
        "",
        "| Model | Passed | Rate | Errored | vs threshold |",
        "|---|---|---|---|---|",
    ]
    for role, m in (("primary", p), ("second", s)):
        pct = round(100 * m["rate"])
        lines.append(
            f"| `{m['model']}` ({role}) | {m['passed']}/{m['total']} | {pct}% | {m['errored']} | "
            f"{_verdict(m['rate'], threshold)} (>= {round(100 * threshold)}%) |"
        )

    worst = max(p["errored"], s["errored"])
    if worst:
        lines += [
            "",
            f"> {worst} fixture(s) never reached the model. The rates above exclude them, "
            "but a run with many errors is thin evidence — re-run before drawing conclusions.",
        ]
    lines.append("")
    return "\n".join(lines)


def render_split(cmp_: dict) -> str:
    """The four-way outcome table — the reason two models are run at all."""
    return "\n".join(
        [
            "| Outcome | Count | Meaning |",
            "|---|---|---|",
            f"| both pass | {len(cmp_['both_pass'])} | nothing to do |",
            f"| only primary | {len(cmp_['only_primary'])} | weaker model's capability gap |",
            f"| only second | {len(cmp_['only_second'])} | noise / flaky discovery |",
            f"| **both fail** | **{len(cmp_['both_fail'])}** | **description problem — actionable** |",
            "",
        ]
    )


def _picked(tool: str | None) -> str:
    return f"`{tool}`" if tool else "(none)"


def _note(primary_reason: str | None, second_reason: str | None) -> str:
    """Only the reasons the picked-tool columns don't already convey.

    `wrong_tool` is the default failure and is fully described by the two
    picked columns, so printing it in every row is noise. `step_cap` and
    `no_tool_call` are different failures — the model never settled, or never
    called anything — and those the columns cannot show.
    """
    interesting = {"step_cap", "no_tool_call"}
    p = primary_reason if primary_reason in interesting else None
    s = second_reason if second_reason in interesting else None
    if p and p == s:
        return f"both: {p}"
    parts = []
    if p:
        parts.append(f"primary: {p}")
    if s:
        parts.append(f"second: {s}")
    return ", ".join(parts)


def render_actionable(cmp_: dict, primary_model: str = "primary", second_model: str = "second") -> str:
    """The both-fail set, with what each model reached for instead.

    Naming only the expected tool says a description is wrong but not what it
    lost to, which is the part you need to rewrite it. The confusion pair is
    the finding.
    """
    if not cmp_["both_fail"]:
        return "No fixture failed for both models.\n"

    # Role-qualified, because the two runs can be the same model (a re-run
    # against itself, or a config A/B), and then two identically named columns
    # tell you nothing about which is which.
    #
    # Owner first: it is the column that decides how urgent the row is. A core
    # description losing to a plugin's is a different problem from two
    # dev-tools descriptions overlapping.
    lines = [
        "#### Descriptions to fix (both models failed)",
        "",
        f"| Owner | Expected tool | Fixture | primary `{primary_model}` picked | "
        f"second `{second_model}` picked | Note |",
        "|---|---|---|---|---|---|",
    ]

    detail = {d["id"]: d for d in cmp_.get("both_fail_detail", [])}

    # Core first, then most-failed tool: 3/3 is a description rewrite, 1/3 an
    # awkward prompt.
    def _rank(kv):
        tool, ids = kv
        return (TIER_ORDER.index(tier_of(tool)) if tier_of(tool) in TIER_ORDER else len(TIER_ORDER), -len(ids), tool)

    for tool, ids in sorted(cmp_["both_fail_by_tool"].items(), key=_rank):
        for fid in ids:
            d = detail.get(fid, {})
            lines.append(
                f"| {tier_of(tool)} | `{tool}` | `{fid}` | {_picked(d.get('primary_selected'))} | "
                f"{_picked(d.get('second_selected'))} | {_note(d.get('primary_reason'), d.get('second_reason'))} |"
            )
    lines.append("")
    return "\n".join(lines)


def render_unmatched(cmp_: dict) -> str:
    if not cmp_["unmatched"]:
        return ""
    return (
        f"> Warning: {len(cmp_['unmatched'])} fixture(s) graded in only one run "
        f"({', '.join(cmp_['unmatched'][:5])}...). The runs are not comparable.\n"
    )


def render(cmp_: dict, threshold: float) -> str:
    """Full standalone report — everything, including the per-model rates."""
    return "\n".join(
        [
            "### Cross-model comparison (discovery mode)",
            "",
            render_rates(cmp_, threshold),
            render_split(cmp_),
            render_actionable(cmp_, cmp_["primary"]["model"], cmp_["second"]["model"]),
            render_unmatched(cmp_),
        ]
    )


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
    # Printed to the step log, not to GITHUB_STEP_SUMMARY: `eval/summary.py`
    # renders the job summary once, at the end, from --output below. Appending
    # here as well would put a second copy of these tables in the middle of it.
    print(render(cmp_, args.min_pass_rate))

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
