#!/usr/bin/env python3
"""Terminal rendering for an eval run.

Separated from eval/runner.py so the numbers can be checked without capturing
stdout. eval/summary.py and eval/compare_runs.py were already built this way —
data in, string out — and they carry the most tests in the repo as a direct
result. These still print rather than return, because they interleave with a
live progress feed; the scoring they render lives in eval/scoring.py.
"""

import json

from eval.scoring import discovery_summary, score, scored, total_tokens
from ownership import OPTIONAL, breakdown

RESET = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[0;36m"


def pct_color(pct: int) -> str:
    return GREEN if pct >= 80 else (YELLOW if pct >= 50 else RED)


def _delta(before: int, after: int, total: int) -> str:
    diff = after - before
    if diff > 0:
        return f"{GREEN}+{diff}{RESET}"
    if diff < 0:
        return f"{RED}{diff}{RESET}"
    return f"{DIM}0{RESET}"


def _arrow(pct_before: int, pct_after: int) -> str:
    diff = pct_after - pct_before
    if diff > 0:
        return f"{GREEN}↑ +{diff}pp{RESET}"
    if diff < 0:
        return f"{RED}↓ {diff}pp{RESET}"
    return f"{DIM}={RESET}"


def _render(result: dict) -> str:
    """One-line progress summary for a finished fixture."""
    if result.get("skipped"):
        return f"{YELLOW}SKIP{RESET}  {result['id']}  ({result['expected_tool']} not registered)"
    if result.get("error"):
        return f"{RED}ERROR{RESET} {result['id']}: {result['error']}"
    status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
    line = f"{status}  {result['id']}  selected={result['selected_tool'] or '(none)'}"
    if result["mode"] == "discovery":
        line += f"  steps={result['steps']}  path={result['discovery_path']}"
        if result.get("attempts", 1) > 1:
            line += f"  (attempts={result['attempts']})"
    return f"{line}  {result.get('latency_s', 0)}s"


def print_single_mode(results: list[dict], mode: str):
    s = score(results)
    total = len(scored(results))
    passed = sum(1 for r in scored(results) if r["passed"])
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}Results: {mode} mode{RESET}")
    print(f"{'=' * 78}")
    pct = round(100 * passed / total) if total else 0
    skip_note = f"  ({skipped} skipped — tool not on this instance)" if skipped else ""
    print(f"  Overall: {pct_color(pct)}{passed}/{total} ({pct}%){RESET}{DIM}{skip_note}{RESET}")
    print(f"\n{BOLD}By category:{RESET}")
    for cat, c in sorted(s["cats"].items()):
        pct = round(100 * c["pass"] / c["total"]) if c["total"] else 0
        print(f"  {cat:<22} {pct_color(pct)}{c['pass']}/{c['total']} ({pct}%){RESET}")


def print_comparison(baseline: list[dict], discovery: list[dict]):
    s_base = score(baseline)
    s_disc = score(discovery)

    total = len(scored(baseline))
    p_base = sum(1 for r in scored(baseline) if r["passed"])
    p_disc = sum(1 for r in scored(discovery) if r["passed"])
    skipped = sum(1 for r in discovery if r.get("skipped"))

    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}Comparison: baseline (full catalogue)  vs  discovery (default surface){RESET}")
    print(f"{'=' * 78}")
    skip_note = f"  ({skipped} skipped — tool not on this instance)" if skipped else ""
    print(
        f"  Overall: {GREEN}{p_base}/{total}{RESET} baseline  →  {GREEN}{p_disc}/{total}{RESET} discovery  "
        f"(Δ {_delta(p_base, p_disc, total)}){DIM}{skip_note}{RESET}"
    )

    # By category
    all_cats = sorted(set(s_base["cats"]) | set(s_disc["cats"]))
    print(f"\n{BOLD}By category:{RESET}")
    print(f"  {'Category':<22} {'Baseline':>12}  {'Discovery':>12}  {'Effect'}")
    print(f"  {'-' * 22} {'-' * 12}  {'-' * 12}  {'-' * 20}")
    for cat in all_cats:
        cb = s_base["cats"].get(cat, {"pass": 0, "total": 0})
        cd = s_disc["cats"].get(cat, {"pass": 0, "total": 0})
        pct_b = round(100 * cb["pass"] / cb["total"]) if cb["total"] else 0
        pct_d = round(100 * cd["pass"] / cd["total"]) if cd["total"] else 0
        print(
            f"  {cat:<22} {pct_color(pct_b)}{cb['pass']}/{cb['total']} ({pct_b}%){RESET:>4}  "
            f"{pct_color(pct_d)}{cd['pass']}/{cd['total']} ({pct_d}%){RESET:>4}  "
            f"{_arrow(pct_b, pct_d)}"
        )

    # Per tool
    all_tools = sorted(set(s_base["tools"]) | set(s_disc["tools"]))
    print(f"\n{BOLD}Per-tool accuracy:{RESET}")
    print(f"  {'Tool':<42} {'Baseline':>10}  {'Discovery':>10}  {'Effect'}")
    print(f"  {'-' * 42} {'-' * 10}  {'-' * 10}  {'-' * 20}")
    for tool in all_tools:
        tb = s_base["tools"].get(tool, {"pass": 0, "total": 0})
        td = s_disc["tools"].get(tool, {"pass": 0, "total": 0})
        pct_b = round(100 * tb["pass"] / tb["total"]) if tb["total"] else 0
        pct_d = round(100 * td["pass"] / td["total"]) if td["total"] else 0
        flag = f"  {RED}⚠{RESET}" if pct_d < 80 else ""
        print(
            f"  {tool:<42} {pct_color(pct_b)}{tb['pass']}/{tb['total']} ({pct_b}%){RESET:>4}  "
            f"{pct_color(pct_d)}{td['pass']}/{td['total']} ({pct_d}%){RESET:>4}  "
            f"{_arrow(pct_b, pct_d)}{flag}"
        )


def print_discovery_block(discovery: list[dict], baseline: list[dict] | None):
    s = discovery_summary(discovery)
    print(f"\n{BOLD}Discovery behaviour:{RESET}")
    print(f"  Avg steps to tool selection: {s['avg_steps']}  (step-cap hit: {s['max_steps_hit']}/{s['fixtures']})")
    dist = "  ".join(f"{k}={v}" for k, v in sorted(s["path_distribution"].items()))
    print(f"  Discovery path: {dist}")
    if s["search_hit_rate"] is not None:
        print(
            f"  tool-search used in {s['search_used']} fixtures; "
            f"expected tool in results: {round(s['search_hit_rate'] * 100)}%"
        )
    if s["toolset_enable_graded"]:
        print(
            f"  toolset-enable graded in {s['toolset_enable_graded']} fixtures; "
            f"correct toolset: {s['toolset_enable_correct']}/{s['toolset_enable_graded']}"
        )
    d_tok = s["tokens"]
    print(f"  Tokens (discovery): {d_tok['input']:,} in / {d_tok['output']:,} out")
    if baseline:
        b_tok = total_tokens(baseline)
        print(f"  Tokens (baseline):  {b_tok['input']:,} in / {b_tok['output']:,} out")
        if b_tok["input"]:
            ratio = round(d_tok["input"] / b_tok["input"], 2)
            print(f"  Input-token ratio discovery/baseline: {ratio}x")

    skipped = [r for r in discovery if r.get("skipped")]
    if skipped:
        names = ", ".join(r["id"] for r in skipped)
        print(f"  {DIM}Skipped (expected tool not registered on this instance): {names}{RESET}")

    failed = [r for r in scored(discovery) if not r["passed"]]
    if failed:
        print(f"\n{BOLD}{RED}Failing in discovery mode:{RESET}")
        for r in failed:
            print(f"\n  [{r['id']}] {r['category']}  ({r.get('fail_reason')})")
            print(f"  {DIM}Prompt:{RESET}   {r['prompt'][:80]}")
            print(f"  {DIM}Expected:{RESET} {GREEN}{r['expected_tool']}{RESET}")
            print(f"  {DIM}Got:{RESET}      {RED}{r['selected_tool']}{RESET}")
            if r["meta_calls"]:
                trail = " → ".join(
                    f"{m['tool']}({json.dumps(m['input'], ensure_ascii=False)[:40]})" for m in r["meta_calls"]
                )
                print(f"  {DIM}Trail:{RESET}    {trail}")
            if r.get("notes"):
                print(f"  {DIM}Notes:{RESET}    {r['notes'][:120]}")
    print(f"\n{'=' * 78}\n")


def print_tier_block(gating: list[dict]) -> None:
    """Per-owning-repo rates, so a bad number points at a repository.

    'admin at 92%' does not say whether the misses were in core, in the
    dev-tools bundle, or in an optional plugin — and those are not the same
    finding.
    """
    tiers = breakdown(gating)
    if len(tiers) < 2:
        return
    print(f"\n{BOLD}By owner:{RESET}")
    for tier, b in tiers.items():
        pct = round(100 * b["rate"])
        note = f"  {DIM}(optional plugin){RESET}" if tier in OPTIONAL else ""
        print(f"  {tier:<20} {pct_color(pct)}{b['passed']}/{b['total']} ({pct}%){RESET}{note}")
