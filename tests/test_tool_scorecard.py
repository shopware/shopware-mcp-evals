"""Per-tool precision, recall and confusion.

The load-bearing cases are the ones that decide whether a tool gets blamed:
`acceptable_tools` must not count as a false positive (a tool would be punished
for being right), the same exclusions as the gate must apply, and an unknown
rate must stay None rather than collapsing to 0.0 — otherwise a never-selected
tool sorts alongside a measurably broken one.
"""

from typing import cast

import pytest

from eval.result_schema import FixtureResult, JsonObject, ToolDef
from eval.tool_scorecard import NO_COVERAGE, collisions, quality, rank_worst, scorecard


def defs(**descriptions: str) -> dict[str, ToolDef]:
    """The `tools` map out of a snapshot, as scorecard() takes it."""
    return {name: ToolDef(name=name, description=text) for name, text in descriptions.items()}


def result(fid: str, expected: str | None, selected: str | None, passed: bool, **extra: object) -> FixtureResult:
    base: JsonObject = {
        "id": fid,
        "expected_tool": expected,
        "selected_tool": selected,
        "passed": passed,
        **extra,
    }
    return cast(FixtureResult, cast(object, base))


def test_recall_and_precision_are_separate_denominators() -> None:
    card = scorecard(
        [
            result("a1", "alpha", "alpha", True),
            result("a2", "alpha", "beta", False),
            result("b1", "beta", "beta", True),
        ]
    )
    # alpha won one of the two fixtures that were its to win.
    assert card["alpha"]["expected_n"] == 2
    assert card["alpha"]["recall"] == 0.5
    # beta was picked twice and was right once — the half the suite could not see.
    assert card["beta"]["selected_n"] == 2
    assert card["beta"]["precision"] == 0.5
    assert card["beta"]["recall"] == 1.0


def test_a_perfect_recall_tool_can_still_have_bad_precision() -> None:
    """The regression this module exists for: greedy descriptions score 100%.

    `alpha` wins every fixture that is its own AND steals beta's. Recall — all
    the old scoring reported — says it is flawless.
    """
    card = scorecard(
        [
            result("a1", "alpha", "alpha", True),
            result("b1", "beta", "alpha", False),
            result("b2", "beta", "alpha", False),
        ]
    )
    assert card["alpha"]["recall"] == 1.0
    assert card["alpha"]["precision"] == pytest.approx(1 / 3)
    assert card["alpha"]["steals_from"] == {"beta": 2}
    assert card["beta"]["confused_with"] == {"alpha": 2}


def test_acceptable_tool_win_is_not_a_false_positive() -> None:
    """Correctness comes from `passed`, never from name equality.

    A fixture listing `acceptable_tools` can be won by a tool that is not
    `expected_tool`. Counting that as a steal would penalise the tool for
    being right, and would invent a collision that does not exist.
    """
    card = scorecard([result("c1", "alpha", "beta", True)])
    assert card["beta"]["precision"] == 1.0
    assert card["beta"]["steals_from"] == {}
    assert card["alpha"]["recall"] == 1.0
    assert card["alpha"]["confused_with"] == {}


def test_skipped_and_errored_are_excluded_like_the_gate() -> None:
    card = scorecard(
        [
            result("ok", "alpha", "alpha", True),
            result("skip", "alpha", None, False, skipped=True),
            result("err", "alpha", None, False, error="500 from server"),
        ]
    )
    assert card["alpha"]["expected_n"] == 1
    assert card["alpha"]["recall"] == 1.0


def test_no_tool_call_is_a_miss_but_blames_nobody() -> None:
    card = scorecard([result("n1", "alpha", None, False, fail_reason="no_tool_call")])
    assert card["alpha"]["recall"] == 0.0
    assert card["alpha"]["confused_with"] == {}


def test_selection_on_a_negative_fixture_is_a_pure_false_positive() -> None:
    """A negative fixture has no expected_tool, so there is no victim to name."""
    card = scorecard([result("neg1", None, "alpha", False)])
    assert card["alpha"]["false_positives_on_negatives"] == 1
    assert card["alpha"]["steals_from"] == {}
    assert card["alpha"]["precision"] == 0.0


def test_declining_a_negative_fixture_touches_no_tool() -> None:
    assert scorecard([result("neg1", None, None, True)]) == {}


def test_unknown_rates_are_none_not_zero() -> None:
    card = scorecard([], catalog=defs(alpha="x"))
    assert card["alpha"]["recall"] is None
    assert card["alpha"]["precision"] is None
    assert card["alpha"]["f1"] is None
    assert card["alpha"]["flags"] == [NO_COVERAGE]


def test_f1_is_none_when_precision_and_recall_are_both_zero() -> None:
    card = scorecard([result("a1", "alpha", "beta", False), result("b1", "beta", "alpha", False)])
    assert card["alpha"]["f1"] is None


def test_catalog_supplies_description_length_and_finds_uncovered_tools() -> None:
    card = scorecard(
        [result("a1", "alpha", "alpha", True)],
        catalog=defs(alpha="four", ghost=""),
    )
    assert card["alpha"]["description_chars"] == 4
    assert card["alpha"]["flags"] == []
    assert card["ghost"]["flags"] == [NO_COVERAGE]


def test_search_rank_is_the_median_over_the_tools_own_fixtures() -> None:
    card = scorecard(
        [
            result("a1", "alpha", "alpha", True, search_rank=1),
            result("a2", "alpha", "alpha", True, search_rank=5),
            result("a3", "alpha", "alpha", True, search_rank=3),
            result("a4", "alpha", "alpha", True),  # older run, no rank recorded
        ]
    )
    assert card["alpha"]["search_rank_p50"] == 3


def test_rank_worst_sorts_unknowns_last() -> None:
    """An unknown rate is not evidence of a problem and must not head the table."""
    card = scorecard(
        [result("a1", "alpha", "beta", False), result("b1", "beta", "beta", True)],
        catalog=defs(ghost=""),
    )
    names = [name for name, _ in rank_worst(card)]
    assert names[-1] == "ghost"
    assert names.index("alpha") < names.index("ghost")


def test_a_tool_that_loses_to_nobody_ranks_worst_not_last() -> None:
    """0% recall with no selections is the worst case, not a missing signal.

    `lost` never got picked at all — the model gave up rather than choosing a
    rival — so it has no precision and no F1. Ranking on F1 alone would file it
    beside the uncovered `ghost` at the bottom of the table, which is exactly
    where nobody looks.
    """
    card = scorecard(
        [
            result("l1", "lost", None, False, fail_reason="no_tool_call"),
            result("f1", "fine", "fine", True),
        ],
        catalog=defs(ghost=""),
    )
    names = [name for name, _ in rank_worst(card)]
    assert names[0] == "lost"
    assert names[-1] == "ghost"
    assert quality(card["lost"]) == 0.0
    assert quality(card["ghost"]) is None


def test_rank_worst_honours_the_limit() -> None:
    card = scorecard([result(f"f{i}", f"t{i}", f"t{i}", True) for i in range(5)])
    assert len(rank_worst(card, 2)) == 2


def test_collisions_report_a_pair_once_and_flag_mutual_confusion() -> None:
    """A mutual mix-up is the strongest signal: both descriptions need work."""
    card = scorecard(
        [
            result("a1", "alpha", "beta", False),
            result("b1", "beta", "alpha", False),
            result("c1", "gamma", "delta", False),
        ]
    )
    found = collisions(card)
    assert found[0]["pair"] == ("alpha", "beta")
    assert found[0]["mutual"] is True
    assert found[0]["total"] == 2
    # One-directional pairs still land, just below the mutual ones.
    assert [c["mutual"] for c in found] == [True, False]


def test_collisions_can_filter_out_one_off_noise() -> None:
    card = scorecard([result("a1", "alpha", "beta", False)])
    assert collisions(card, min_count=2) == []


def test_precision_is_about_the_first_pick_not_the_eventual_outcome() -> None:
    """The regression recovery introduces: a fixture that recovers still passes,
    but the tool that lost it on the first pick must not be credited with a good
    selection — nor the tool that stole it excused."""
    card = scorecard(
        [
            result("r1", "alpha", "beta", True, first_tool_correct=False)  # recovered on a later attempt
        ]
    )

    assert card["beta"]["precision"] == 0.0, "beta was picked and was wrong, however it ended"
    assert card["alpha"]["recall"] == 0.0, "alpha did not win its own fixture on the pick that counts"
    assert card["alpha"]["confused_with"] == {"beta": 1}


def test_reports_predating_recovery_still_read_correctly() -> None:
    """`passed` meant exactly first-pick-correct before the recovery loop, so an
    older report must not be reinterpreted."""
    card = scorecard([result("old", "alpha", "alpha", True)])

    assert card["alpha"]["recall"] == 1.0
    assert card["alpha"]["precision"] == 1.0
