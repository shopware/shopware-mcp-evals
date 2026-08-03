"""Cost accounting.

The load-bearing cases are the ones that decide whether a number is trustworthy:
an unpriced model must read as a gap rather than as free, the price table must
cover every model the runner can actually resolve, and cost-per-passing-fixture
has to behave the way the metric is meant to — a run that spends more to convert
failures into passes gets *cheaper* per unit of signal.
"""

from pathlib import Path
from typing import cast

import pytest
import yaml

from eval import cost as C
from eval.result_schema import FixtureResult, JsonObject, ModelPrice, Pricing, TokenCounts
from eval.runner import PROVIDER_DEFAULTS

PRICES: ModelPrice = {"input": 1.0, "output": 10.0, "cached_input": 0.1}
PRICING: Pricing = {"verified": "2026-07-30", "models": {"m1": PRICES}, "ci": {"runner_usd_per_minute": 0.008}}


def bare(fid: str, **extra: object) -> FixtureResult:
    """A fixture that never reached the model, so it carries no `tokens` at all —
    which is the case token_totals has to ignore rather than filter."""
    base: JsonObject = {"id": fid, "passed": False, **extra}
    return cast(FixtureResult, cast(object, base))


def result(fid: str, passed: bool = True, tokens: TokenCounts | None = None, **extra: object) -> FixtureResult:
    base: JsonObject = {
        "id": fid,
        "passed": passed,
        "tokens": tokens if tokens is not None else TokenCounts(input=1000, cached_input=0, output=100),
        **extra,
    }
    return cast(FixtureResult, cast(object, base))


# ---------------------------------------------------------------------------
# The price table must cover what the runner can actually resolve
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("provider,model", sorted(PROVIDER_DEFAULTS.items()))
def test_every_default_model_is_priced(provider: str, model: str) -> None:
    """The check that catches a new default model before it silently costs $0.00.

    Runtime degrades gracefully for an unpriced model; this is where it is meant
    to hurt instead.
    """
    assert C.prices_for(model, C.load_pricing()) is not None, (
        f"{provider} defaults to {model!r}, which has no entry in pricing.yaml"
    )


def test_the_committed_price_table_is_complete_and_dated() -> None:
    pricing = C.load_pricing()
    assert pricing.get("verified"), "pricing.yaml must carry the date it was last checked"
    for name, prices in (pricing.get("models") or {}).items():
        for field in ("input", "output", "cached_input"):
            assert isinstance(prices.get(field), int | float), f"{name} is missing a numeric {field}"


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------
def test_each_token_bucket_is_billed_at_its_own_rate() -> None:
    assert C.cost_usd({"input": 1_000_000, "output": 0, "cached_input": 0}, PRICES) == 1.0
    assert C.cost_usd({"input": 0, "output": 1_000_000, "cached_input": 0}, PRICES) == 10.0
    assert C.cost_usd({"input": 0, "output": 0, "cached_input": 1_000_000}, PRICES) == 0.1


def test_a_cached_prefix_is_cheaper_than_the_same_tokens_at_full_price() -> None:
    """The discount is real money and lands whether or not we asked for it."""
    full = C.cost_usd(TokenCounts(input=1_000_000, output=0), PRICES)
    cached = C.cost_usd(TokenCounts(input=0, output=0, cached_input=1_000_000), PRICES)
    assert cached < full


def test_an_unknown_token_bucket_is_billed_at_nothing_rather_than_crashing() -> None:
    """A provider adding a bucket must not take down the nightly."""
    assert C.cost_usd(cast(TokenCounts, cast(object, {"input": 1_000_000, "reasoning": 5_000_000})), PRICES) == 1.0


def test_token_totals_ignore_fixtures_that_never_reached_the_model() -> None:
    totals = C.token_totals([result("a"), bare("skipped", skipped=True), bare("errored", error="boom")])
    assert totals == {"input": 1000, "cached_input": 0, "output": 100}


# ---------------------------------------------------------------------------
# run_cost
# ---------------------------------------------------------------------------
def test_an_unpriced_model_reads_as_a_gap_not_as_free() -> None:
    """Reporting $0.00 would be a lie that looks like good news."""
    out = C.run_cost([result("a")], "not-in-the-table", PRICING)

    assert out["priced"] is False
    assert out["total_usd"] is None
    assert out["usd_per_fixture"] is None
    assert out["tokens"]["input"] == 1000, "volume is still known even when the price is not"


def test_cost_per_passing_fixture_rewards_converting_failures() -> None:
    """The metric's whole point: spending more for more signal is better value.

    Both runs cost the same in total. The second converts one failure into a
    pass, so its price per unit of signal must fall.
    """
    one_pass = C.run_cost([result("a", True), result("b", False)], "m1", PRICING)
    two_passes = C.run_cost([result("a", True), result("b", True)], "m1", PRICING)

    assert one_pass["total_usd"] == two_passes["total_usd"]
    two, one = two_passes["usd_per_passing_fixture"], one_pass["usd_per_passing_fixture"]
    assert two is not None and one is not None and two < one


def test_a_run_with_no_passes_has_no_unit_price() -> None:
    out = C.run_cost([result("a", False)], "m1", PRICING)
    assert out["usd_per_passing_fixture"] is None
    assert out["usd_per_fixture"] is not None


def test_skipped_fixtures_are_out_of_the_denominators() -> None:
    out = C.run_cost([result("a"), result("s", passed=False, skipped=True)], "m1", PRICING)
    assert out["graded"] == 1 and out["passed"] == 1


def test_unverified_prices_are_flagged_through_to_the_caller() -> None:
    pricing: Pricing = {"models": {"m1": PRICES | {"unverified": True}}}
    assert C.run_cost([result("a")], "m1", pricing)["unverified"] is True
    assert C.run_cost([result("a")], "m1", PRICING)["unverified"] is False


def test_run_cost_reports_the_distribution_not_just_the_total() -> None:
    results = [
        result("a", latency_s=1.0, payload_bytes=100, surface_tokens=50, surface_tokens_peak=50),
        result("b", latency_s=9.0, payload_bytes=900, surface_tokens=50, surface_tokens_peak=400),
    ]
    out = C.run_cost(results, "m1", PRICING)

    assert out["latency_p50"] == 1.0
    assert out["latency_p95"] == 9.0
    assert out["payload_bytes_p95"] == 900
    assert out["surface_tokens_peak"] == 400, "the peak is what later turns actually paid for"


def test_missing_distribution_fields_do_not_invent_zeroes() -> None:
    """Older reports predate these fields; absent must stay absent."""
    out = C.run_cost([result("a")], "m1", PRICING)
    assert out["latency_p50"] is None and out["payload_bytes_p95"] is None


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------
def test_percentile_reports_a_value_that_actually_occurred() -> None:
    """Nearest-rank, not interpolated: a p95 no fixture had invites the wrong
    kind of scrutiny."""
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert C.percentile(values, 95) in values
    assert C.percentile(values, 50) == 3


def test_percentile_of_an_empty_series_is_none() -> None:
    assert C.percentile([], 50) is None


def test_percentile_of_a_single_value() -> None:
    assert C.percentile([7], 95) == 7


# ---------------------------------------------------------------------------
# Job-level rollup
# ---------------------------------------------------------------------------
def test_combining_runs_sums_dollars_and_tokens() -> None:
    runs = [C.run_cost([result("a")], "m1", PRICING), C.run_cost([result("b")], "m1", PRICING)]
    combined = C.combine(runs)

    assert combined["tokens"]["input"] == 2000
    first = runs[0]["total_usd"]
    assert first is not None
    assert combined["total_usd"] == pytest.approx(2 * first)
    assert combined["complete"] is True


def test_one_unpriced_run_makes_the_job_total_incomplete_without_losing_volume() -> None:
    runs = [C.run_cost([result("a")], "m1", PRICING), C.run_cost([result("b")], "unknown", PRICING)]
    combined = C.combine(runs)

    assert combined["complete"] is False
    assert combined["unpriced_models"] == ["unknown"]
    assert combined["tokens"]["input"] == 2000, "tokens are known even when prices are not"


def test_combine_surfaces_unverified_models_for_the_reader() -> None:
    pricing: Pricing = {"models": {"m1": PRICES | {"unverified": True}}}
    combined = C.combine([C.run_cost([result("a")], "m1", pricing)])
    assert combined["unverified_models"] == ["m1"]


def test_combine_of_nothing_is_zero_and_complete() -> None:
    assert C.combine([])["total_usd"] == 0.0
    assert C.combine([])["complete"] is True


# ---------------------------------------------------------------------------
# CI minutes
# ---------------------------------------------------------------------------
def test_ci_minutes_are_billed_at_the_configured_rate() -> None:
    assert C.ci_cost_usd(10, PRICING) == pytest.approx(0.08)


def test_ci_cost_is_none_when_no_duration_was_measured() -> None:
    assert C.ci_cost_usd(None, PRICING) is None


def test_ci_cost_is_zero_when_runners_are_free() -> None:
    assert C.ci_cost_usd(10, {"ci": {"runner_usd_per_minute": 0.0}}) == 0.0
    assert C.ci_cost_usd(10, {}) == 0.0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def test_a_missing_or_broken_price_table_reads_as_unpriced_not_as_free(tmp_path: Path) -> None:
    assert C.load_pricing(tmp_path / "absent.yaml") == {}

    broken = tmp_path / "broken.yaml"
    broken.write_text("{ this is not: valid: yaml")
    assert C.load_pricing(broken) == {}

    assert C.prices_for("anything", {}) is None


def test_load_pricing_defaults_to_the_committed_table() -> None:
    assert C.load_pricing().get("models"), "the default path must resolve to pricing.yaml"


def test_the_committed_table_parses_as_yaml() -> None:
    assert isinstance(yaml.safe_load(C.DEFAULT_PRICING.read_text()), dict)


def test_a_model_used_by_two_suites_is_named_once() -> None:
    """The primary grades both admin and Store. Naming it twice in a warning
    reads as two separate problems."""
    runs = [C.run_cost([result("a")], "unknown", PRICING), C.run_cost([result("b")], "unknown", PRICING)]
    assert C.combine(runs)["unpriced_models"] == ["unknown"]

    pricing: Pricing = {"models": {"m1": PRICES | {"unverified": True}}}
    dupes = [C.run_cost([result("a")], "m1", pricing), C.run_cost([result("b")], "m1", pricing)]
    assert C.combine(dupes)["unverified_models"] == ["m1"]
