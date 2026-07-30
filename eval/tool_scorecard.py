#!/usr/bin/env python3
"""Per-tool quality, including the half the suite could not previously see.

Every fixture asks "is tool X picked when X is the right answer?" — that is
recall, and it is all `eval/scoring.py` reports. It says nothing about how often
X is picked when X is *wrong*, which is the failure mode an over-broad
description actually produces: a tool described as "search anything in the shop"
wins its own three fixtures and quietly steals its siblings'. On recall alone it
scores 100%.

The missing half costs nothing to compute. Every wrong selection already
recorded in `selected_tool` is a false positive for whichever tool was picked,
so precision, F1 and a confusion matrix fall out of runs that already happened —
including the ones committed under results/. No new fixtures, no new API calls.

Correctness is read from `passed`, never from name equality, because a fixture
may legitimately be won by a tool in its `acceptable_tools` list. Counting that
as a false positive would penalise a tool for being right.

Pure functions, like eval/scoring.py: records in, numbers out. Rendering lives
in eval/summary.py.
"""

from statistics import median

from eval.scoring import executed

# Reported for a tool the catalogue has but no fixture exercises. The ≥3-prompts
# invariant in tests/test_fixtures.py should make this impossible; if one shows
# up, a tool shipped without coverage and the invariant has a hole.
NO_COVERAGE = "no_coverage"


def _ratio(numerator: int, denominator: int) -> float | None:
    """Rate, or None when there is nothing to divide by.

    None rather than 0.0 on purpose: a tool that was never selected has an
    *unknown* precision, not a perfect or a terrible one, and the two must not
    sort together in the scorecard.
    """
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return 2 * precision * recall / (precision + recall)


def scorecard(results: list[dict], catalog: dict[str, dict] | None = None) -> dict[str, dict]:
    """Per-tool recall, precision, F1 and confusion over one run's results.

    `catalog` is the `tools` map from tool-history/latest.json. It is optional —
    without it the scorecard covers only tools that appear in the results, so a
    tool nothing ever picked or expected is invisible. With it, such a tool is
    reported with NO_COVERAGE, which is the interesting case.

    Errored and skipped fixtures are dropped via `executed`, so the denominators
    here match the ones the gate uses.
    """
    graded = executed(results)
    tools: dict[str, dict] = {}

    def bucket(name: str) -> dict:
        return tools.setdefault(
            name,
            {
                "expected_n": 0,
                "expected_passed": 0,
                "selected_n": 0,
                "selected_correct": 0,
                "confused_with": {},
                "steals_from": {},
                "false_positives_on_negatives": 0,
                "search_ranks": [],
            },
        )

    for r in graded:
        expected = r.get("expected_tool")
        selected = r.get("selected_tool")
        # The whole scorecard is a claim about the FIRST pick, recall included:
        # description quality is what drives which tool the model reaches for,
        # and a tool that only wins on the second attempt has not earned recall
        # credit. Since the recovery loop landed, `passed` means "got there in
        # the end", so it is no longer that claim — `first_tool_correct` is.
        # Reports written before recovery existed carry no such field and fall
        # back to `passed`, which meant exactly this at the time.
        first_correct = r.get("first_tool_correct")
        won = bool(r.get("passed")) if first_correct is None else bool(first_correct)

        # Recall side: this fixture is a test of `expected`.
        if expected:
            entry = bucket(expected)
            entry["expected_n"] += 1
            if won:
                entry["expected_passed"] += 1
            elif selected:
                # Something else won a fixture that belonged to `expected`.
                entry["confused_with"][selected] = entry["confused_with"].get(selected, 0) + 1
            rank = r.get("search_rank")
            if rank is not None:
                entry["search_ranks"].append(rank)

        # Precision side: this fixture is an observation about `selected`.
        if selected:
            entry = bucket(selected)
            entry["selected_n"] += 1
            if won:
                entry["selected_correct"] += 1
            elif expected:
                entry["steals_from"][expected] = entry["steals_from"].get(expected, 0) + 1
            else:
                # A negative fixture: no tool should have been called at all, so
                # there is no victim to attribute the steal to. Counted on its
                # own line because it is the cleanest evidence of over-triggering
                # the suite can produce.
                entry["false_positives_on_negatives"] += 1

    for name in catalog or {}:
        bucket(name)

    out: dict[str, dict] = {}
    for name, e in tools.items():
        recall = _ratio(e["expected_passed"], e["expected_n"])
        precision = _ratio(e["selected_correct"], e["selected_n"])
        definition = (catalog or {}).get(name) or {}
        out[name] = {
            "expected_n": e["expected_n"],
            "recall": recall,
            "selected_n": e["selected_n"],
            "precision": precision,
            "f1": _f1(precision, recall),
            # Sorted worst-first so the rendered cell leads with the biggest
            # offender rather than whichever name hashed first.
            "confused_with": dict(sorted(e["confused_with"].items(), key=lambda kv: -kv[1])),
            "steals_from": dict(sorted(e["steals_from"].items(), key=lambda kv: -kv[1])),
            "false_positives_on_negatives": e["false_positives_on_negatives"],
            "search_rank_p50": median(e["search_ranks"]) if e["search_ranks"] else None,
            "description_chars": len(definition.get("description") or ""),
            "flags": [NO_COVERAGE] if e["expected_n"] == 0 else [],
        }
    return out


def quality(entry: dict) -> float | None:
    """Best available 0–1 quality estimate for one tool, or None if there is none.

    F1 when both halves are known. Recall alone when the tool was never picked —
    which is not a missing signal but a bad one: a tool that loses its own
    fixtures to nobody (the model gave up, or answered in prose) has 0% recall
    and no precision at all. Ranking on F1 alone would file that beside a tool
    with no fixtures and bury the worst case in the table.
    """
    if entry["f1"] is not None:
        return entry["f1"]
    return entry["recall"]


def rank_worst(card: dict[str, dict], limit: int | None = None) -> list[tuple[str, dict]]:
    """Scorecard entries worst-first, for rendering.

    Only a tool with neither rate — never expected, never picked — sorts last.
    That is a genuine unknown rather than evidence of a problem, so it must not
    displace the tools that are measurably bad.
    """
    ordered = sorted(
        card.items(),
        key=lambda kv: (quality(kv[1]) is None, quality(kv[1]) if quality(kv[1]) is not None else 0, kv[0]),
    )
    return ordered[:limit] if limit else ordered


def collisions(card: dict[str, dict], min_count: int = 1) -> list[dict]:
    """Confusion as unordered pairs, so a mutual mix-up is reported once.

    A pair that trades misses in both directions is the strongest signal the
    scorecard produces: two descriptions that each attract the other's prompts
    need differentiating from each other, not fixing in isolation. `mutual` is
    what distinguishes that from one tool simply being greedy.
    """
    pairs: dict[tuple[str, str], dict] = {}
    for name, entry in card.items():
        for other, count in entry["confused_with"].items():
            key = (name, other) if name < other else (other, name)
            slot = pairs.setdefault(key, {"pair": key, "total": 0, "directions": {}})
            slot["total"] += count
            slot["directions"][f"{other} won {name}"] = count
    for slot in pairs.values():
        slot["mutual"] = len(slot["directions"]) > 1
    return sorted(
        (p for p in pairs.values() if p["total"] >= min_count),
        key=lambda p: (not p["mutual"], -p["total"]),
    )
