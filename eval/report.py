#!/usr/bin/env python3
"""Terminal rendering for an eval run.

Separated from eval/runner.py so the numbers can be checked without capturing
stdout. eval/summary.py and eval/compare_runs.py were already built this way —
data in, string out — and they carry the most tests in the repo as a direct
result. These still print rather than return, because they interleave with a
live progress feed; the scoring they render lives in eval/scoring.py.
"""

import json

from eval.result_schema import AttemptRecord, FixtureResult
from eval.scoring import discovery_summary, executed, score, scored
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


def render_line(result: FixtureResult) -> str:
    """One-line progress summary for a finished fixture."""
    if result.get("skipped"):
        return f"{YELLOW}SKIP{RESET}  {result['id']}  ({result['expected_tool']} not registered)"
    if result.get("error"):
        return f"{RED}ERROR{RESET} {result['id']}: {result.get('error', '')}"
    status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
    line = f"{status}  {result['id']}  selected={result['selected_tool'] or '(none)'}"
    if result["mode"] == "discovery":
        line += f"  steps={result.get('steps', 0)}  path={result.get('discovery_path', '')}"
        if result.get("attempts", 1) > 1:
            line += f"  (attempts={result.get('attempts', 1)})"
    return f"{line}  {result.get('latency_s', 0)}s"


def print_single_mode(results: list[FixtureResult], mode: str):
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

    # Per-tool accuracy. This used to be the three-column baseline/discovery/effect
    # table; dropping baseline mode left the one column that meant anything. It is
    # the reason to read this report at all: a tool below 80% is flagged, and the
    # "Failing" block further down names what got picked instead.
    print(f"\n{BOLD}Per-tool accuracy:{RESET}")
    print(f"  {'Tool':<42} {'Passed':>10}")
    print(f"  {'-' * 42} {'-' * 10}")
    for tool, t in sorted(s["tools"].items()):
        pct = round(100 * t["pass"] / t["total"]) if t["total"] else 0
        flag = f"  {RED}⚠{RESET}" if pct < 80 else ""
        print(f"  {tool:<42} {pct_color(pct)}{t['pass']}/{t['total']} ({pct}%){RESET}{flag}")


def print_discovery_block(discovery: list[FixtureResult]):
    s = discovery_summary(discovery)
    print(f"\n{BOLD}Discovery behaviour:{RESET}")
    cap = f"{s.get('max_steps_hit', 0)}/{s.get('fixtures', 0)}"
    print(f"  Avg steps to tool selection: {s.get('avg_steps', 0)}  (step-cap hit: {cap})")
    dist = "  ".join(f"{k}={v}" for k, v in sorted(s.get("path_distribution", {}).items()))
    print(f"  Discovery path: {dist}")
    if s.get("search_hit_rate") is not None:
        print(
            f"  tool-search used in {s.get('search_used', 0)} fixtures; "
            f"expected tool in results: {round((s.get('search_hit_rate') or 0) * 100)}%"
        )
    if s.get("toolset_enable_graded"):
        print(
            f"  toolset-enable graded in {s.get('toolset_enable_graded', 0)} fixtures; "
            f"correct toolset: {s.get('toolset_enable_correct', 0)}/{s.get('toolset_enable_graded', 0)}"
        )
    d_tok = s.get("tokens", {})
    print(f"  Tokens: {d_tok.get('input', 0):,} in / {d_tok.get('output', 0):,} out")

    skipped = [r for r in discovery if r.get("skipped")]
    if skipped:
        names = ", ".join(r["id"] for r in skipped)
        print(f"  {DIM}Skipped (expected tool not registered on this instance): {names}{RESET}")

    # `executed`, not `scored`. A fixture that errored before reaching the model
    # is missing data, and the gate already excludes it on exactly that basis —
    # listing it here as a failure meant the section and the verdict counted
    # different things. It showed up as `negative_carrier_label (None)`: no
    # reason, no trail, nothing anyone could act on, while the gate line right
    # underneath said "1/96 fixtures never reached the model".
    errored = [r for r in scored(discovery) if r.get("error")]
    if errored:
        names = ", ".join(f"{r['id']} ({str(r.get('error', ''))[:60]})" for r in errored)
        print(f"  {DIM}Errored before reaching the model (excluded from the gate): {names}{RESET}")

    failed = [r for r in executed(discovery) if not r["passed"]]
    if failed:
        print(f"\n{BOLD}{RED}Failing in discovery mode:{RESET}")
        for r in failed:
            print(f"\n  [{r['id']}] {r['category']}  ({r.get('fail_reason')})")
            print(f"  {DIM}Prompt:{RESET}   {r['prompt'][:80]}")
            # A negative fixture names no tool: the expectation is that nothing
            # is called, so printing a bare "None" here would read as a bug in
            # the fixture rather than the finding it actually is.
            expected = r.get("expected_tool") or "(no tool — nothing should match)"
            print(f"  {DIM}Expected:{RESET} {GREEN}{expected}{RESET}")
            print(f"  {DIM}Got:{RESET}      {RED}{r['selected_tool']}{RESET}")
            # What it actually tried, and what came back. A fixture that named
            # the right tool and had the call rejected looks identical to one
            # that never chose anything unless this is printed.
            attempts: list[AttemptRecord] = r.get("attempted_tools") or []
            for attempt in attempts:
                if attempt.get("ok"):
                    continue
                bits = [attempt["tool"]]
                if not attempt.get("executed"):
                    bits.append("not executed")
                if attempt.get("reason"):
                    bits.append(str(attempt.get("reason")))
                if attempt.get("error"):
                    bits.append(f"server said: {attempt.get('error', '')[:100]}")
                print(f"  {DIM}Tried:{RESET}    {' — '.join(bits)}")
            if r.get("meta_calls", []):
                trail = " → ".join(
                    f"{m['tool']}({json.dumps(m['input'], ensure_ascii=False)[:40]})" for m in r.get("meta_calls", [])
                )
                print(f"  {DIM}Trail:{RESET}    {trail}")
            if r.get("notes"):
                print(f"  {DIM}Notes:{RESET}    {r.get('notes', '')[:120]}")
    print(f"\n{'=' * 78}\n")


def print_tier_block(gating: list[FixtureResult]) -> None:
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
