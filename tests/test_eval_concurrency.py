"""Unit tests for the eval runner's fixture-level parallelism.

These cover the ordering/limits contract only — no LLM or MCP calls.
"""

import threading
import time

from eval import runner as E

FIXTURES = [{"id": f"f{i}", "expected_tool": "t", "prompt": "p", "category": "c"} for i in range(12)]


def _worker(fixture):
    return {"id": fixture["id"], "mode": "baseline", "passed": True, "_line": fixture["id"]}


def test_results_keep_fixture_order_not_completion_order():
    """Fast fixtures finish first, but the report must stay in fixture order."""

    def worker(fixture):
        # Reverse the natural completion order: later fixtures finish sooner.
        time.sleep((len(FIXTURES) - int(fixture["id"][1:])) * 0.002)
        return _worker(fixture)

    results = E.run_fixtures_concurrently(FIXTURES, worker, workers=6)
    assert [r["id"] for r in results] == [f["id"] for f in FIXTURES]


def test_sequential_path_matches_parallel_path():
    seq = E.run_fixtures_concurrently(FIXTURES, _worker, workers=1)
    par = E.run_fixtures_concurrently(FIXTURES, _worker, workers=8)
    assert [r["id"] for r in seq] == [r["id"] for r in par]
    assert len(seq) == len(FIXTURES)


def test_concurrency_limit_is_respected():
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(fixture):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return _worker(fixture)

    E.run_fixtures_concurrently(FIXTURES, worker, workers=3)
    assert peak <= 3


def test_every_fixture_produces_a_result():
    results = E.run_fixtures_concurrently(FIXTURES, _worker, workers=5)
    assert len(results) == len(FIXTURES)
    assert all(r is not None for r in results)


# --- error/skip record shape -------------------------------------------------
def test_error_result_shape_baseline():
    rec = E.error_result(FIXTURES[0], "baseline", RuntimeError("boom"))
    assert rec["passed"] is False
    assert rec["error"] == "boom"
    assert "steps" not in rec  # discovery-only fields stay out of baseline records


def test_error_result_shape_discovery():
    rec = E.error_result(FIXTURES[0], "discovery", RuntimeError("boom"))
    assert rec["steps"] == 0
    assert rec["discovery_path"] == "none"
    assert rec["meta_calls"] == []


def test_count_rate_limited_recognises_throttle_shapes():
    results = [
        {"error": "429 Too Many Requests"},
        {"error": "RateLimitError: rate limit exceeded"},
        {"error": "quota exhausted for this model"},
        {"error": "connection reset by peer"},  # not throttling
        {"passed": True},  # no error at all
    ]
    assert E.count_rate_limited(results) == 3


def test_count_rate_limited_catches_github_models_403():
    """GitHub Models throttles with 403 + an anti-scraping notice, not 429 — and
    half the fixtures surface only as a bare 'Error code: 403'."""
    results = [
        {"error": "Error code: 403"},
        {"error": "Too many requests. For more on scraping GitHub ... terms-of-service"},
    ]
    assert E.count_rate_limited(results) == 2


def test_count_rate_limited_handles_none_and_empty():
    assert E.count_rate_limited(None) == 0
    assert E.count_rate_limited([]) == 0


# write_ci_summary became write_summary_row (it emits a JSON row for
# eval/summary.py instead of appending its own markdown table). Its tests moved
# to tests/test_eval_summary_row.py.


def test_github_provider_defaults_to_a_non_openai_publisher():
    """The second validator's value is being an independent implementation, so
    its default must not be another OpenAI model."""
    model = E.PROVIDER_DEFAULTS["github"]
    assert not model.startswith("openai/")
    assert "/" in model  # GitHub Models ids are publisher-qualified
    assert E.GITHUB_MODELS_BASE_URL.startswith("https://")


def test_every_provider_choice_has_a_default_model():
    """The invariant, not a hardcoded list: argparse accepts a provider, then
    resolve_model indexes PROVIDER_DEFAULTS by it. A choice with no entry is a
    KeyError after the run has already started — which is exactly how `lmstudio`
    first failed, having been added to the choices and not to the defaults."""
    choices = next(a.choices for a in E.build_parser()._actions if a.dest == "provider")

    assert set(choices) == set(E.PROVIDER_DEFAULTS)


def test_every_default_model_is_priced():
    """An unpriced model reports "unpriced" rather than a cost, which quietly
    turns the job total into an estimate. Free is a price; unknown is not."""
    from eval.cost import load_pricing, prices_for

    pricing = load_pricing()
    unpriced = [m for m in E.PROVIDER_DEFAULTS.values() if prices_for(m, pricing) is None]

    assert not unpriced, f"no pricing.yaml entry for {unpriced}"


def test_render_marks_pass_fail_skip():
    passed = {"id": "a", "mode": "baseline", "passed": True, "selected_tool": "x", "latency_s": 1}
    failed = {"id": "b", "mode": "baseline", "passed": False, "selected_tool": None, "latency_s": 1}
    skipped = {"id": "c", "mode": "baseline", "skipped": True, "expected_tool": "x"}
    assert "PASS" in E._render(passed)
    assert "FAIL" in E._render(failed)
    assert "SKIP" in E._render(skipped)


# ---------------------------------------------------------------------------
# executed() — the gate must not read a broken server as a bad model
# ---------------------------------------------------------------------------


def _r(fid, passed, *, skipped=False, error=None):
    rec = {"id": fid, "passed": passed, "skipped": skipped, "expected_tool": "t", "category": "c"}
    if error:
        rec["error"] = error
    return rec


def test_executed_excludes_errored_and_skipped_fixtures():
    results = [
        _r("ok", True),
        _r("wrong", False),
        _r("boom", False, error="500 Server Error"),
        _r("throttled", False, error="429 Client Error: Too Many Requests"),
        _r("absent", False, skipped=True),
    ]

    assert [r["id"] for r in E.scored(results)] == ["ok", "wrong", "boom", "throttled"]
    assert [r["id"] for r in E.executed(results)] == ["ok", "wrong"]


def test_executed_rate_matches_the_real_regression():
    """The 45-fixture run that read as 53% was 89% over fixtures that ran.

    24 passed, 3 genuinely wrong, 18 errored. Averaging the errors in gives
    24/45 = 53%; excluding them gives 24/27 = 89%.
    """
    results = (
        [_r(f"p{i}", True) for i in range(24)]
        + [_r(f"f{i}", False) for i in range(3)]
        + [_r(f"e{i}", False, error="500 Server Error") for i in range(18)]
    )

    scored_rate = sum(1 for r in E.scored(results) if r["passed"]) / len(E.scored(results))
    executed_rate = sum(1 for r in E.executed(results) if r["passed"]) / len(E.executed(results))

    assert round(scored_rate * 100) == 53
    assert round(executed_rate * 100) == 89
