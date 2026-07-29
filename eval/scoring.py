#!/usr/bin/env python3
"""Turning a list of fixture results into numbers and a verdict.

Pure functions: a list of result records in, counts and booleans out. No I/O, no
printing, no provider calls. That is deliberate — every one of these decides
whether CI goes red, and the exclusions in particular are load-bearing:
`executed` dropping transport errors is the difference between reporting a model
at 53% and at its real 89%.

Kept apart from eval/report.py, which renders these numbers, and from
eval/runner.py, which produces the records. The same split is why
eval/summary.py and eval/compare_runs.py are the best-tested code here.
"""

from ownership import core_rate


def is_correct(selected_tool: str | None, fixture: dict) -> bool:
    """A selection is correct if it is the expected tool or any tool listed in
    the fixture's optional `acceptable_tools` (for genuinely multi-valid prompts)."""
    if selected_tool is None:
        return False
    return selected_tool == fixture["expected_tool"] or selected_tool in fixture.get("acceptable_tools", [])


def scored(results: list[dict]) -> list[dict]:
    """Results that count toward pass/fail — skipped fixtures are excluded."""
    return [r for r in results if not r.get("skipped")]


def executed(results: list[dict]) -> list[dict]:
    """Scored results that actually reached the model.

    A transport error — the server answering 500, or throttling with 429 — is
    missing data, not a wrong answer, so it must not be averaged in as a
    failure. One local run read as 53% when 18 of its 21 "failures" were the
    server erroring; the rate over fixtures that ran was 89%. Errors are held
    against the run separately, via the error budget in the gate.
    """
    return [r for r in scored(results) if not r.get("error")]


def score(results: list[dict]) -> dict:
    """Return per-tool and per-category pass counts (skipped fixtures excluded)."""
    tools: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    for r in scored(results):
        t = r["expected_tool"]
        c = r["category"]
        tools.setdefault(t, {"pass": 0, "total": 0})
        cats.setdefault(c, {"pass": 0, "total": 0})
        tools[t]["total"] += 1
        cats[c]["total"] += 1
        if r["passed"]:
            tools[t]["pass"] += 1
            cats[c]["pass"] += 1
    return {"tools": tools, "cats": cats}


def total_tokens(results: list[dict]) -> dict:
    agg = {"input": 0, "output": 0}
    for r in results:
        t = r.get("tokens") or {}
        agg["input"] += t.get("input", 0)
        agg["output"] += t.get("output", 0)
    return agg


def count_rate_limited(results: list[dict] | None) -> int:
    """Fixtures whose error looks like provider throttling.

    Worth separating from ordinary failures: a throttled fixture says nothing
    about tool-description quality, it just means we outran the quota. This is
    the number that decides whether a free-tier validator is viable at our
    fixture count.
    """
    # GitHub Models answers a throttled request with HTTP 403 and an
    # anti-scraping notice rather than a 429, so matching only on 429/"rate
    # limit" undercounts it — half the fixtures show up as a bare "Error code:
    # 403". A 403 here is throttling, not authorization: a bad credential fails
    # every fixture identically at 401 before any work happens.
    needles = ("429", "403", "rate limit", "rate_limit", "too many requests", "quota", "scraping")
    return sum(1 for r in results or [] if any(n in str(r.get("error", "")).lower() for n in needles))


def discovery_summary(discovery: list[dict]) -> dict:
    graded = scored(discovery)
    n = len(graded)
    passed = sum(1 for r in graded if r["passed"])
    steps = [r["steps"] for r in graded]
    paths: dict[str, int] = {}
    for r in graded:
        paths[r["discovery_path"]] = paths.get(r["discovery_path"], 0) + 1
    search_used = [r for r in graded if r["search_hit"] is not None]
    search_hits = sum(1 for r in search_used if r["search_hit"])
    toolset_graded = [r for r in graded if r["enabled_correct_toolset"] is not None]
    toolset_correct = sum(1 for r in toolset_graded if r["enabled_correct_toolset"])
    return {
        "fixtures": n,
        "skipped": sum(1 for r in discovery if r.get("skipped")),
        "passed": passed,
        "avg_steps": round(sum(steps) / n, 2) if n else 0,
        "max_steps_hit": sum(1 for r in graded if r.get("fail_reason") == "step_cap"),
        "path_distribution": paths,
        "search_used": len(search_used),
        "search_hit_rate": round(search_hits / len(search_used), 2) if search_used else None,
        "toolset_enable_graded": len(toolset_graded),
        "toolset_enable_correct": toolset_correct,
        "tokens": total_tokens(graded),
    }


def gate_verdict(results, min_pass_rate, min_core_pass_rate, max_error_rate) -> dict:
    """Decide whether a run passes, and on which of the three independent axes.

    Kept pure and separate from main() so the policy is testable without a
    server. The three failure modes are deliberately not folded together:

    * quality  — the overall rate fell below threshold;
    * core     — core specifically fell below threshold, on its own denominator;
    * validity — too many fixtures never reached the model, so the run is
                 missing data rather than reporting a bad model. Folding errors
                 into the rate is how an 89% run once got read as 53%.

    Core needs its own axis because the aggregate spans four repositories: with
    90 admin fixtures and a single 90% gate, nine core misses still read PASS as
    long as merchant-tools and dev-tools are clean — backwards, since the plugin
    numbers are the ones we can afford to lose.

    `min_core_pass_rate=None` means "same bar as the overall rate". That is a
    deliberate default: the win here is core getting its own denominator, and
    raising the bar is a decision to make once the per-tier rates have been
    observed, not an aspiration invented up front.
    """
    graded = scored(results)
    gating = executed(results)
    errored = len(graded) - len(gating)
    error_rate = errored / len(graded) if graded else 0.0

    passed = sum(1 for r in gating if r["passed"])
    rate = passed / len(gating) if gating else 1.0

    core_passed, core_total, core_pct = core_rate(gating)
    min_core = min_core_pass_rate if min_core_pass_rate is not None else min_pass_rate

    quality_ok = rate >= min_pass_rate
    core_ok = core_pct >= min_core
    run_valid = error_rate <= max_error_rate
    return {
        "graded": graded,
        "gating": gating,
        "errored": errored,
        "error_rate": error_rate,
        "passed": passed,
        "rate": rate,
        "core_passed": core_passed,
        "core_total": core_total,
        "core_rate": core_pct,
        "min_core": min_core,
        "quality_ok": quality_ok,
        "core_ok": core_ok,
        "run_valid": run_valid,
        "ok": quality_ok and core_ok and run_valid,
    }
