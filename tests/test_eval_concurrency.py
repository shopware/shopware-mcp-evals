"""Unit tests for the eval runner's fixture-level parallelism.

These cover the ordering/limits contract only — no LLM or MCP calls.
"""

import importlib.util
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# eval/run.py and functional/run.py are both called `run`. Load this one under a
# distinct module name so importing it cannot shadow the functional runner in
# sys.modules (which silently broke tests/test_run.py once already).
_spec = importlib.util.spec_from_file_location("eval_run", ROOT / "eval" / "run.py")
E = importlib.util.module_from_spec(_spec)
sys.modules["eval_run"] = E
_spec.loader.exec_module(E)

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


def test_github_provider_defaults_to_a_non_openai_publisher():
    """The second validator's value is being an independent implementation, so
    its default must not be another OpenAI model."""
    model = E.PROVIDER_DEFAULTS["github"]
    assert not model.startswith("openai/")
    assert "/" in model  # GitHub Models ids are publisher-qualified
    assert E.GITHUB_MODELS_BASE_URL.startswith("https://")


def test_every_provider_choice_has_a_default_model():
    assert set(E.PROVIDER_DEFAULTS) == {"anthropic", "openai", "github"}


def test_render_marks_pass_fail_skip():
    passed = {"id": "a", "mode": "baseline", "passed": True, "selected_tool": "x", "latency_s": 1}
    failed = {"id": "b", "mode": "baseline", "passed": False, "selected_tool": None, "latency_s": 1}
    skipped = {"id": "c", "mode": "baseline", "skipped": True, "expected_tool": "x"}
    assert "PASS" in E._render(passed)
    assert "FAIL" in E._render(failed)
    assert "SKIP" in E._render(skipped)
