"""Scoring and the gate verdict.

`gate_verdict` decides whether CI goes red and had no direct test: it was only
exercised through a full run against a live server and a paid provider. The
cases below are the ones where an off-by-one stops the gate gating at all —
a rate landing exactly on the threshold, an empty run, and core failing while
the aggregate passes.
"""

from eval import scoring as S

CORE = "shopware-entity-read"
DEV = "swag-dev-tools-log-search"
PLUGIN = "merchant-order-summary"


def r(fid, passed=True, tool=CORE, category="unambiguous", **over):
    return {"id": fid, "passed": passed, "expected_tool": tool, "category": category, **over}


def verdict(results, min_pass_rate=0.9, min_core_pass_rate=None, max_error_rate=0.1):
    return S.gate_verdict(results, min_pass_rate, min_core_pass_rate, max_error_rate)


# ---------------------------------------------------------------------------
# is_correct
# ---------------------------------------------------------------------------
def test_the_expected_tool_is_correct():
    assert S.is_correct(CORE, {"expected_tool": CORE})


def test_no_selection_is_never_correct():
    """A model that called nothing has not answered; it must not read as a pass."""
    assert S.is_correct(None, {"expected_tool": CORE}) is False


def test_a_different_tool_is_wrong():
    assert S.is_correct(DEV, {"expected_tool": CORE}) is False


def test_an_acceptable_alternative_counts_for_multi_valid_prompts():
    fixture = {"expected_tool": CORE, "acceptable_tools": [DEV]}

    assert S.is_correct(DEV, fixture)


def test_acceptable_tools_is_optional():
    assert S.is_correct(DEV, {"expected_tool": CORE}) is False


# ---------------------------------------------------------------------------
# scored / executed
# ---------------------------------------------------------------------------
def test_skipped_fixtures_are_excluded_from_scoring():
    """A tool absent from this instance must not count as a failure."""
    results = [r("a"), r("b", passed=False, skipped=True)]

    assert [x["id"] for x in S.scored(results)] == ["a"]


def test_errored_fixtures_are_excluded_from_the_gating_set():
    """A 500 or a 429 is missing data, not a wrong answer. Averaging it in as a
    failure once read an 89% run as 53%."""
    results = [r("a"), r("e", passed=False, error="500 Server Error")]

    assert [x["id"] for x in S.executed(results)] == ["a"]
    # Still graded, so the error budget can see it.
    assert len(S.scored(results)) == 2


def test_a_skipped_and_errored_fixture_is_counted_once():
    assert S.executed([r("x", skipped=True, error="500")]) == []


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
def test_score_counts_per_tool_and_per_category():
    results = [
        r("a", tool=CORE, category="unambiguous"),
        r("b", passed=False, tool=CORE, category="disambiguation"),
        r("c", tool=DEV, category="unambiguous"),
    ]

    out = S.score(results)

    assert out["tools"][CORE] == {"pass": 1, "total": 2}
    assert out["tools"][DEV] == {"pass": 1, "total": 1}
    assert out["cats"]["unambiguous"] == {"pass": 2, "total": 2}
    assert out["cats"]["disambiguation"] == {"pass": 0, "total": 1}


def test_score_excludes_skipped():
    out = S.score([r("a"), r("b", skipped=True)])

    assert out["tools"][CORE]["total"] == 1


def test_score_of_nothing_is_empty_not_an_error():
    assert S.score([]) == {"tools": {}, "cats": {}}


# ---------------------------------------------------------------------------
# total_tokens
# ---------------------------------------------------------------------------
def test_tokens_sum_across_results():
    results = [r("a", tokens={"input": 10, "output": 2}), r("b", tokens={"input": 5, "output": 1})]

    assert S.total_tokens(results) == {"input": 15, "output": 3}


def test_tokens_tolerate_a_result_that_never_reached_the_model():
    """An errored fixture has no token record at all."""
    assert S.total_tokens([r("a"), r("b", tokens=None)]) == {"input": 0, "output": 0}


# ---------------------------------------------------------------------------
# gate_verdict — the three axes
# ---------------------------------------------------------------------------
def test_a_clean_run_passes_on_all_three_axes():
    v = verdict([r(f"f{i}") for i in range(10)])

    assert (v["quality_ok"], v["core_ok"], v["run_valid"], v["ok"]) == (True, True, True, True)
    assert v["rate"] == 1.0


def test_a_rate_exactly_on_the_threshold_passes():
    """`>=`, not `>`. At 90 fixtures a 90% gate must accept exactly 81."""
    v = verdict([r(f"p{i}") for i in range(9)] + [r("f", passed=False)], min_pass_rate=0.9)

    assert v["rate"] == 0.9
    assert v["quality_ok"] is True


def test_one_fixture_below_the_threshold_fails():
    v = verdict([r(f"p{i}") for i in range(8)] + [r("f1", passed=False), r("f2", passed=False)])

    assert v["rate"] == 0.8
    assert v["quality_ok"] is False
    assert v["ok"] is False


def test_core_can_fail_while_the_aggregate_passes():
    """The reason core has its own axis: with four repos in one denominator,
    core misses hide behind clean plugin numbers."""
    results = [r(f"c{i}", passed=False, tool=CORE) for i in range(2)]
    results += [r(f"p{i}", tool=PLUGIN) for i in range(18)]

    v = verdict(results, min_pass_rate=0.9)

    assert v["rate"] == 0.9 and v["quality_ok"] is True
    assert v["core_rate"] == 0.0 and v["core_ok"] is False
    assert v["ok"] is False, "a clean plugin suite must not carry a broken core"


def test_min_core_defaults_to_the_overall_threshold():
    v = verdict([r("a")], min_pass_rate=0.75, min_core_pass_rate=None)

    assert v["min_core"] == 0.75


def test_min_core_can_be_raised_above_the_overall_threshold():
    results = [r("c1"), r("c2", passed=False)]

    v = verdict(results, min_pass_rate=0.4, min_core_pass_rate=0.9)

    assert v["quality_ok"] is True
    assert v["core_rate"] == 0.5 and v["core_ok"] is False


def test_a_suite_with_no_core_fixtures_does_not_fail_the_core_gate():
    """The store suite is almost entirely UCP; it has no core fixtures to fail."""
    v = verdict([r("u", tool="shopware-ucp-cart-get", passed=False)], min_pass_rate=0.0)

    assert v["core_total"] == 0
    assert v["core_rate"] == 1.0 and v["core_ok"] is True


# ---------------------------------------------------------------------------
# gate_verdict — the error budget
# ---------------------------------------------------------------------------
def test_errors_do_not_count_against_the_rate():
    """9 passes and 1 transport error is a 100% run over what actually ran."""
    results = [r(f"p{i}") for i in range(9)] + [r("e", passed=False, error="500 Server Error")]

    v = verdict(results)

    assert v["rate"] == 1.0
    assert v["errored"] == 1
    assert v["error_rate"] == 0.1


def test_the_error_budget_is_inclusive_at_its_limit():
    results = [r(f"p{i}") for i in range(9)] + [r("e", passed=False, error="429")]

    assert verdict(results, max_error_rate=0.1)["run_valid"] is True


def test_too_many_errors_invalidate_the_run_even_at_a_perfect_rate():
    """Otherwise two fixtures out of twenty reporting 100% reads as a green run
    on almost no evidence."""
    results = [r(f"p{i}") for i in range(2)] + [r(f"e{i}", passed=False, error="500") for i in range(8)]

    v = verdict(results, max_error_rate=0.1)

    assert v["rate"] == 1.0 and v["quality_ok"] is True
    assert v["error_rate"] == 0.8 and v["run_valid"] is False
    assert v["ok"] is False


# ---------------------------------------------------------------------------
# gate_verdict — degenerate input
# ---------------------------------------------------------------------------
def test_an_empty_run_reports_a_rate_of_one():
    """Documenting a sharp edge rather than endorsing it: nothing to grade reads
    as a pass, which is why eval/runner.load_fixtures refuses a filter that
    matches no fixtures instead of letting the run reach here."""
    v = verdict([])

    assert v["rate"] == 1.0 and v["error_rate"] == 0.0
    assert v["ok"] is True


def test_a_run_of_only_skips_is_treated_as_empty():
    v = verdict([r("a", skipped=True), r("b", skipped=True)])

    assert v["graded"] == [] and v["gating"] == []
    assert v["ok"] is True


def test_a_run_of_only_errors_is_invalid_rather_than_scored():
    v = verdict([r("e1", passed=False, error="500"), r("e2", passed=False, error="500")])

    assert v["rate"] == 1.0, "no fixture reached the model, so there is no rate"
    assert v["error_rate"] == 1.0 and v["run_valid"] is False
    assert v["ok"] is False


def test_the_verdict_carries_the_sets_the_report_renders_from():
    results = [r("a"), r("s", skipped=True), r("e", passed=False, error="500")]

    v = verdict(results, max_error_rate=1.0)

    assert [x["id"] for x in v["graded"]] == ["a", "e"]
    assert [x["id"] for x in v["gating"]] == ["a"]
    assert v["passed"] == 1


# ---------------------------------------------------------------------------
# Negative fixtures — the right answer is that no tool applies
# ---------------------------------------------------------------------------


def negative(**over):
    return {"id": "neg", "category": "negative", "expect_no_tool": True, **over}


def test_declining_a_negative_fixture_passes():
    assert S.is_correct(None, negative(), fail_reason="no_tool_call") is True


def test_calling_anything_on_a_negative_fixture_fails():
    """The whole point: an over-broad description bites here and nowhere else."""
    assert S.is_correct("shopware-entity-search", negative()) is False


def test_running_out_of_steps_is_not_a_decision_to_decline():
    """Both leave selected_tool empty, but only one is the model concluding.

    Without this, a negative fixture passes for timing out — the model is still
    rummaging when the budget runs out and gets credit for restraint.
    """
    assert S.is_correct(None, negative(), fail_reason="step_cap") is False
    assert S.is_correct(None, negative(), fail_reason=None) is True


def test_ordinary_fixtures_ignore_the_fail_reason():
    fixture = {"expected_tool": "alpha"}
    assert S.is_correct("alpha", fixture, fail_reason="step_cap") is True
    assert S.is_correct(None, fixture, fail_reason=None) is False


def test_is_negative_keys_on_the_flag_not_the_category():
    assert S.is_negative(negative()) is True
    assert S.is_negative({"category": "negative"}) is False
    assert S.is_negative({"expected_tool": "alpha"}) is False


def test_a_negative_has_no_row_in_the_per_tool_table_but_counts_in_its_category():
    """It is a statement about the catalogue, not about one tool."""
    out = S.score(
        [
            {"expected_tool": "alpha", "category": "unambiguous", "passed": True},
            {"expected_tool": None, "category": "negative", "passed": False},
        ]
    )

    assert list(out["tools"]) == ["alpha"]
    assert out["cats"]["negative"] == {"pass": 0, "total": 1}


# ---------------------------------------------------------------------------
# Recovery aggregates
# ---------------------------------------------------------------------------
def attempt(fid, first_try, recovered=False, wrong=0, steps_to_correct=None, **extra):
    return {
        "id": fid,
        "category": "unambiguous",
        "expected_tool": "alpha",
        "passed": first_try or recovered,
        "first_try": first_try,
        "recovered": recovered,
        "wrong_calls": wrong,
        "steps_to_correct": steps_to_correct,
        "attempted_tools": [{"tool": "alpha", "correct": first_try}],
        "search_hit": None,
        "steps": 1,
        "discovery_path": "direct",
        "enabled_correct_toolset": None,
        **extra,
    }


def test_recovery_rate_is_measured_over_the_fixtures_that_missed_first():
    """Denominator is the misses, not everything — otherwise a mostly-first-try
    suite dilutes it to meaninglessness."""
    out = S.recovery_summary(
        [
            attempt("a", first_try=True),
            attempt("b", first_try=True),
            attempt("c", first_try=False, recovered=True, wrong=1, steps_to_correct=2),
            attempt("d", first_try=False, wrong=3),
        ]
    )

    assert out["first_try_rate"] == 0.5
    assert out["recovery_rate"] == 0.5, "one of the two misses recovered"
    assert out["recovered"] == 1
    assert out["avg_wrong_calls"] == 1.0
    assert out["avg_steps_to_correct"] == 2


def test_a_suite_that_never_misses_has_no_recovery_rate():
    out = S.recovery_summary([attempt("a", first_try=True)])
    assert out["first_try_rate"] == 1.0
    assert out["recovery_rate"] is None


def test_recovery_aggregates_are_absent_for_reports_that_predate_them():
    """Older fixtures carry none of these fields and must not be counted as
    first-try by default."""
    assert S.recovery_summary([{"id": "old", "passed": True}]) == {}


def test_unexecuted_and_forced_dry_runs_are_counted():
    out = S.recovery_summary(
        [
            attempt("a", first_try=True, execution="executed", dry_run_forced=True),
            attempt("b", first_try=True, execution="skipped_unsafe"),
            attempt("c", first_try=True, execution="skipped_unclassified"),
        ]
    )

    assert out["dry_run_forced"] == 1
    assert out["unexecuted"] == 2


def test_discovery_summary_carries_the_recovery_numbers():
    out = S.discovery_summary([attempt("a", first_try=False, recovered=True, wrong=1)])
    assert out["first_try_rate"] == 0.0 and out["recovered"] == 1


def test_a_fixture_that_never_answered_does_not_crash_the_summary():
    """`execution` is present and None whenever the model never produced an
    answer, and a .get() default only applies to a *missing* key — so
    None.startswith took down the whole report after every fixture had run."""
    out = S.recovery_summary([attempt("a", first_try=False, execution=None)])

    assert out["unexecuted"] == 0
    assert out["first_try_rate"] == 0.0


def test_discovery_summary_over_records_the_runner_actually_produces(tmp_path):
    """Hand-built records kept diverging from real ones — the crash above got
    through because no scoring test used a record the runner had made."""
    from eval import runner as E

    E.mcp_init_orig = None
    record = {
        "id": "f1",
        "category": "unambiguous",
        "expected_tool": "alpha",
        "selected_tool": None,
        "passed": False,
        "first_tool_correct": None,
        "first_try": False,
        "recovered": False,
        "attempted_tools": [],
        "wrong_calls": 0,
        "steps_to_correct": None,
        "execution": None,
        "dry_run_forced": False,
        "steps": 6,
        "meta_calls": [],
        "discovery_path": "none",
        "search_hit": None,
        "enabled_correct_toolset": None,
        "tokens": {"input": 1, "cached_input": 0, "output": 1},
    }

    out = S.discovery_summary([record])

    assert out["first_try_rate"] == 0.0
    assert out["unexecuted"] == 0
