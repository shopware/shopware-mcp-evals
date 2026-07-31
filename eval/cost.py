#!/usr/bin/env python3
"""What a run costs, in dollars and in the things dollars are made of.

`latency_s` and `tokens` have been recorded per fixture since the suite was
written and never added up, so nobody could answer "what does a pipeline run
cost?" — which meant every argument about scope was settled on intuition. The
measured answer for the current suite is ~3.1M input tokens and $1-2 per run,
and knowing that changes which trade-offs are worth making.

The number that matters most is not the total. It is **cost per passing
fixture**: a run that doubles in price to convert four failures into passes got
cheaper per unit of signal, and a total alone hides that.

Two rules about missing prices, which pull in opposite directions on purpose:

  * At runtime, an unpriced model degrades to "not priced" and the run
    continues. Cost is a reporting feature and must never be the thing that
    turns a nightly red.
  * In the unit tests, an unpriced model is a hard failure. That is where a new
    default model gets caught, long before it can quietly report $0.00.

Pure functions: records and a price table in, numbers out. No I/O beyond
load_pricing, no printing.
"""

import math
from pathlib import Path

import yaml

# Prices are quoted per million tokens; usage is counted in tokens.
PER_TOKENS = 1_000_000

DEFAULT_PRICING = Path(__file__).resolve().parents[1] / "pricing.yaml"


def load_pricing(path: str | Path | None = None) -> dict:
    """Read the price table. A missing or broken file yields an empty table,
    which renders as "not priced" rather than as free."""
    try:
        return yaml.safe_load(Path(path or DEFAULT_PRICING).read_text()) or {}
    except OSError, yaml.YAMLError:
        return {}


ZERO_PRICES = {"input": 0.0, "output": 0.0, "cached_input": 0.0}


def prices_for(model: str, pricing: dict) -> dict | None:
    return (pricing.get("models") or {}).get(model)


def token_totals(results: list[dict]) -> dict:
    """Sum the three token buckets across every fixture that reached the model.

    Skipped and errored fixtures carry no `tokens` key — they never got that
    far — so they contribute nothing rather than needing to be filtered out.
    """
    totals = {"input": 0, "cached_input": 0, "output": 0}
    for r in results or []:
        for bucket, count in (r.get("tokens") or {}).items():
            totals[bucket] = totals.get(bucket, 0) + count
    return totals


def cost_usd(tokens: dict, prices: dict) -> float:
    """Dollar cost of one token bucket set at the given rates."""
    return sum(count * (prices.get(bucket) or 0.0) for bucket, count in (tokens or {}).items()) / PER_TOKENS


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. None for an empty series.

    Nearest-rank rather than interpolating: every series here is small (tens of
    fixtures), and reporting a p95 latency no fixture actually had invites
    exactly the wrong kind of scrutiny.
    """
    if not values:
        return None
    ordered = sorted(values)
    # Nearest rank is ceil(p/100 * n), 1-indexed. `round(x + 0.5)` looks like
    # ceiling and is not: at exactly 1.0 it rounds to 2, which reported the
    # larger of two values as the median.
    rank = math.ceil(p / 100 * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def _series(results: list[dict], key: str) -> list[float]:
    return [r[key] for r in results or [] if r.get(key) is not None]


# Providers that bill nothing because the model runs on the machine doing the
# asking. Keyed by provider rather than model name: a local server serves
# whatever is loaded, so the model name is discovered at runtime and can never be
# enumerated in pricing.yaml ahead of time.
FREE_PROVIDERS = frozenset({"lmstudio"})


def run_cost(results: list[dict], model: str, pricing: dict, provider: str | None = None) -> dict:
    """Cost and volume for one run.

    `priced` is False when the model has no entry in the table — the caller
    renders that as an explicit gap. Reporting $0.00 instead would be a lie that
    looks like good news.

    A local provider is the one case where $0.00 is the truth rather than a gap,
    and it has to be decided by provider: the run records the model LM Studio
    actually served (`qwen/qwen3.6-35b-a3b`), which is the honest thing to report
    and is never going to appear in a price table.
    """
    graded = [r for r in results or [] if not r.get("skipped")]
    passed = [r for r in graded if r.get("passed")]
    tokens = token_totals(results)
    prices = ZERO_PRICES if provider in FREE_PROVIDERS else prices_for(model, pricing)

    total = cost_usd(tokens, prices) if prices else None
    return {
        "model": model,
        "priced": prices is not None,
        "unverified": bool(prices.get("unverified")) if prices else False,
        "verified": pricing.get("verified"),
        "tokens": tokens,
        "total_usd": total,
        "graded": len(graded),
        "passed": len(passed),
        # Per *passing* fixture is the honest unit price of signal: a run that
        # costs more but converts failures into passes is better value, and the
        # total alone says the opposite.
        "usd_per_fixture": (total / len(graded)) if total is not None and graded else None,
        "usd_per_passing_fixture": (total / len(passed)) if total is not None and passed else None,
        "latency_p50": percentile(_series(graded, "latency_s"), 50),
        "latency_p95": percentile(_series(graded, "latency_s"), 95),
        "payload_bytes_p50": percentile(_series(graded, "payload_bytes"), 50),
        "payload_bytes_p95": percentile(_series(graded, "payload_bytes"), 95),
        "surface_tokens_p50": percentile(_series(graded, "surface_tokens"), 50),
        "surface_tokens_peak": max(_series(graded, "surface_tokens_peak"), default=None),
    }


def ci_cost_usd(minutes: float | None, pricing: dict) -> float | None:
    """Runner cost for the job, or None when no duration was supplied.

    Free on public repositories, so the configured rate is 0.0 and this
    contributes nothing — but it is wired up because "what did this run cost"
    should not silently mean "the LLM half of what this run cost".
    """
    if minutes is None:
        return None
    return minutes * float((pricing.get("ci") or {}).get("runner_usd_per_minute", 0.0) or 0.0)


def combine(runs: list[dict]) -> dict:
    """Roll several runs' costs into the job-level headline.

    Unpriced runs still contribute their token volume — tokens are always
    known — but leave the dollar total incomplete, which the caller says out
    loud rather than rounding away.
    """
    tokens = {"input": 0, "cached_input": 0, "output": 0}
    total = 0.0
    unpriced = []
    for run in runs or []:
        for bucket, count in (run.get("tokens") or {}).items():
            tokens[bucket] = tokens.get(bucket, 0) + count
        if run.get("total_usd") is None:
            unpriced.append(run.get("model"))
        else:
            total += run["total_usd"]
    # Deduped, order preserved: the same model runs more than once per job (the
    # primary grades both the admin and the Store suite), and naming it twice in
    # a warning reads as two separate problems.
    return {
        "tokens": tokens,
        "total_usd": total,
        "complete": not unpriced,
        "unpriced_models": list(dict.fromkeys(unpriced)),
        "unverified_models": list(dict.fromkeys(r["model"] for r in runs or [] if r.get("unverified"))),
    }
