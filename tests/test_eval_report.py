"""Terminal rendering of an eval run.

These print rather than return, so they are driven through capsys. The colour
codes are stripped before asserting — what matters is the numbers and which
lines appear, not the escape sequences.

Rendering is low-consequence line by line, but not free of arithmetic:
print_single_mode computes a rate per category and per tool. A wrong denominator
there misreports a run without failing anything.
"""

import re

import pytest

from eval import report as R

STRIP = re.compile(r"\033\[[0-9;]*m")


def plain(capsys) -> str:
    return STRIP.sub("", capsys.readouterr().out)


def base(fid, passed=True, tool="shopware-entity-read", category="unambiguous", **over):
    """A minimal record. Its mode is deliberately not "discovery", so the tests
    below can show which lines _render adds only for a discovery record."""
    return {
        "id": fid,
        "mode": "other",
        "passed": passed,
        "expected_tool": tool,
        "selected_tool": tool if passed else "other-tool",
        "category": category,
        "latency_s": 1.5,
        "tokens": {"input": 100, "output": 10},
        **over,
    }


def disc(fid, passed=True, **over):
    return (
        base(fid, passed)
        | {
            "mode": "discovery",
            "steps": 2,
            "discovery_path": "toolsets",
            "search_hit": None,
            "enabled_correct_toolset": passed,
            "enabled_toolsets": ["entity"],
            "meta_calls": [],
            "prompt": "Read the product.",
            "notes": "",
            "fail_reason": None if passed else "wrong_tool",
        }
        | over
    )


# ---------------------------------------------------------------------------
# pct_color
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pct,expected", [(100, R.GREEN), (80, R.GREEN), (79, R.YELLOW), (50, R.YELLOW), (49, R.RED)])
def test_pct_color_thresholds_are_inclusive_at_the_boundary(pct, expected):
    assert R.pct_color(pct) == expected


# ---------------------------------------------------------------------------
# _render — the per-fixture progress line
# ---------------------------------------------------------------------------
def test_render_marks_a_pass_with_the_selected_tool():
    line = STRIP.sub("", R._render(base("f1")))

    assert line.startswith("PASS")
    assert "selected=shopware-entity-read" in line


def test_render_shows_none_when_the_model_called_nothing():
    line = STRIP.sub("", R._render(base("f1", passed=False, selected_tool=None)))

    assert "selected=(none)" in line


def test_render_reports_a_skip_with_the_absent_tool():
    line = STRIP.sub("", R._render(base("f1", skipped=True)))

    assert line.startswith("SKIP")
    assert "not registered" in line


def test_render_reports_a_transport_error_instead_of_a_verdict():
    line = STRIP.sub("", R._render(base("f1", passed=False, error="500 Server Error")))

    assert line.startswith("ERROR")
    assert "500 Server Error" in line


def test_render_adds_steps_and_path_only_in_discovery_mode():
    assert "steps=" not in STRIP.sub("", R._render(base("f1")))
    assert "steps=2  path=toolsets" in STRIP.sub("", R._render(disc("f1")))


def test_render_flags_a_fixture_that_needed_its_retry():
    """A pass on the second attempt is a flaky fixture, not a clean one."""
    line = STRIP.sub("", R._render(disc("f1", attempts=2)))

    assert "(attempts=2)" in line
    assert "(attempts" not in STRIP.sub("", R._render(disc("f2", attempts=1)))


# ---------------------------------------------------------------------------
# print_single_mode
# ---------------------------------------------------------------------------
def test_single_mode_reports_the_overall_rate_and_categories(capsys):
    results = [base("a"), base("b", passed=False), base("c", category="chain")]

    R.print_single_mode(results, "discovery")
    out = plain(capsys)

    assert "Results: discovery mode" in out
    assert "Overall: 2/3 (67%)" in out
    assert "unambiguous" in out and "1/2 (50%)" in out
    assert "chain" in out and "1/1 (100%)" in out


def test_single_mode_notes_skips_outside_the_denominator(capsys):
    R.print_single_mode([base("a"), base("s", skipped=True)], "discovery")
    out = plain(capsys)

    assert "Overall: 1/1 (100%)" in out
    assert "(1 skipped — tool not on this instance)" in out


def test_single_mode_omits_the_skip_note_when_nothing_skipped(capsys):
    R.print_single_mode([base("a")], "discovery")

    assert "skipped" not in plain(capsys)


def test_single_mode_of_an_empty_run_does_not_divide_by_zero(capsys):
    R.print_single_mode([], "discovery")

    assert "Overall: 0/0 (0%)" in plain(capsys)


def test_single_mode_reports_accuracy_per_tool(capsys):
    """The per-tool table is what the report is read for: it says which tool the
    misses were against, not just that the run lost a fixture."""
    R.print_single_mode([base("a"), base("b", passed=False), base("c", tool="shopware-media-upload")], "discovery")
    out = plain(capsys)

    assert "Per-tool accuracy:" in out
    assert "shopware-entity-read" in out and "1/2 (50%)" in out
    assert "shopware-media-upload" in out and "1/1 (100%)" in out


def test_single_mode_flags_a_tool_below_eighty_percent(capsys):
    R.print_single_mode([base("a", passed=False)], "discovery")

    assert "⚠" in plain(capsys)


def test_single_mode_does_not_flag_a_tool_at_full_marks(capsys):
    R.print_single_mode([base("a")], "discovery")

    assert "⚠" not in plain(capsys)


# ---------------------------------------------------------------------------
# print_discovery_block
# ---------------------------------------------------------------------------
def test_discovery_block_reports_steps_and_path_distribution(capsys):
    R.print_discovery_block([disc("a"), disc("b", discovery_path="search")])
    out = plain(capsys)

    assert "Avg steps to tool selection: 2.0" in out
    assert "search=1" in out and "toolsets=1" in out


def test_discovery_block_reports_search_hit_rate_only_when_search_was_used(capsys):
    R.print_discovery_block([disc("a")])
    assert "tool-search used" not in plain(capsys)

    R.print_discovery_block([disc("a", search_hit=True), disc("b", search_hit=False)])
    out = plain(capsys)
    assert "tool-search used in 2 fixtures" in out
    assert "50%" in out


def test_discovery_block_reports_toolset_routing_when_graded(capsys):
    R.print_discovery_block([disc("a"), disc("b", passed=False)])
    out = plain(capsys)

    assert "toolset-enable graded in 2 fixtures" in out
    assert "correct toolset: 1/2" in out


def test_discovery_block_totals_the_tokens(capsys):
    """Token cost is what the run is compared against between commits, so the
    total has to be summed over the fixtures rather than taken from one."""
    R.print_discovery_block([disc("a", tokens={"input": 300, "output": 20}), disc("b")])

    assert "Tokens: 400 in / 30 out" in plain(capsys)


def test_discovery_block_names_the_skipped_fixtures(capsys):
    R.print_discovery_block([disc("a"), disc("s1", skipped=True), disc("s2", skipped=True)])
    out = plain(capsys)

    assert "Skipped (expected tool not registered on this instance): s1, s2" in out


def test_discovery_block_details_each_failure(capsys):
    failed = disc(
        "f1",
        passed=False,
        category="disambiguation",
        prompt="Show me the orders.",
        notes="entity-search vs merchant-order-summary",
        meta_calls=[{"tool": "shopware-toolsets-list", "input": {}}],
    )

    R.print_discovery_block([failed])
    out = plain(capsys)

    assert "Failing in discovery mode:" in out
    assert "[f1] disambiguation  (wrong_tool)" in out
    assert "Show me the orders." in out
    assert "Expected: shopware-entity-read" in out
    assert "Got:      other-tool" in out
    assert "Trail:    shopware-toolsets-list({})" in out
    assert "entity-search vs merchant-order-summary" in out


def test_discovery_block_lists_an_errored_fixture_apart_from_the_failures(capsys):
    """A fixture that never reached the model is missing data, and the gate
    already excludes it. Listing it as a failure made the section and the
    verdict count different things — and with no reason, no trail and no
    attempts, the entry said nothing anyone could act on."""
    errored = disc("e1", passed=False, fail_reason=None, error="Expecting value: line 1 column 1 (char 0)")

    R.print_discovery_block([disc("a"), errored])
    out = plain(capsys)

    assert "Errored before reaching the model (excluded from the gate): e1 (Expecting value" in out
    assert "Failing in discovery mode" not in out


def test_discovery_block_omits_the_trail_when_there_were_no_meta_calls(capsys):
    R.print_discovery_block([disc("f1", passed=False, meta_calls=[])])

    assert "Trail:" not in plain(capsys)


def test_discovery_block_says_nothing_about_failures_on_a_clean_run(capsys):
    R.print_discovery_block([disc("a")])

    assert "Failing in discovery mode" not in plain(capsys)


# ---------------------------------------------------------------------------
# print_tier_block
# ---------------------------------------------------------------------------
def test_tier_block_splits_the_rate_by_owning_repository(capsys):
    gating = [
        base("c1", tool="shopware-entity-read"),
        base("c2", passed=False, tool="shopware-entity-read"),
        base("d1", tool="swag-dev-tools-log-search"),
    ]

    R.print_tier_block(gating)
    out = plain(capsys)

    assert "By owner:" in out
    assert "core" in out and "1/2 (50%)" in out
    assert "dev-tools" in out and "1/1 (100%)" in out


def test_tier_block_marks_optional_plugins(capsys):
    gating = [base("c1", tool="shopware-entity-read"), base("m1", tool="merchant-order-summary")]

    R.print_tier_block(gating)

    assert "(optional plugin)" in plain(capsys)


def test_tier_block_is_silent_when_everything_has_one_owner(capsys):
    """A single-tier table restates the overall rate, so it is not printed."""
    R.print_tier_block([base("c1"), base("c2")])

    assert plain(capsys) == ""


def test_tier_block_is_silent_on_an_empty_gating_set(capsys):
    R.print_tier_block([])

    assert plain(capsys) == ""


def test_a_failing_negative_fixture_says_what_was_expected(capsys):
    """Printing a bare "None" would read as a broken fixture, not a finding."""
    R.print_discovery_block(
        [
            disc(
                "neg1",
                passed=False,
                expected_tool=None,
                selected_tool="shopware-entity-search",
                category="negative",
                fail_reason="wrong_tool",
            )
        ]
    )

    out = capsys.readouterr().out
    assert "(no tool — nothing should match)" in out
    assert "shopware-entity-search" in out
