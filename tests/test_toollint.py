"""Static catalogue lint.

The checks here were chosen by measuring the real catalogue, so the tests that
matter most are the ones pinning *why a check is shaped the way it is*: the
uniform properties stay catalogue-level facts rather than becoming thirty
identical findings, and similarity stays out of the findings list entirely.
"""

import json
from pathlib import Path
from typing import cast

import pytest

import toollint as T
from eval.result_schema import JsonObject, Snapshot, ToolDef, as_object

# Long enough to clear MIN_DESCRIPTION_CHARS and carrying a trigger phrase, so
# a tool built from the default is genuinely clean and a test that expects no
# findings is testing the check rather than the fixture.
CLEAN = (
    "Use this when the caller needs the thing done. It covers the ordinary case "
    "end to end, returns the identifiers the caller will need next, and leaves "
    "the neighbouring operations to their own dedicated tools rather than "
    "trying to serve every request itself."
)


def tool(name: str, description: str = CLEAN, schema: JsonObject | None = None) -> ToolDef:
    return ToolDef(name=name, description=description, inputSchema=schema if schema is not None else {})


def catalogue(*tools: ToolDef) -> Snapshot:
    """A snapshot carrying only the field toollint reads."""
    return cast(Snapshot, cast(object, {"tools": list(tools)}))


def test_a_description_that_never_says_when_to_call_is_flagged() -> None:
    long_what = "Ranks products by units sold in a date range. " * 6
    assert "no_trigger_phrase" in T.lint_tool(tool("t", long_what))


@pytest.mark.parametrize(
    "phrasing",
    [
        "Use this when the caller wants a report. " * 6,
        "Call it when a cart already exists. " * 6,
        "Search the catalogue. If you need pricing, prefer the other tool. " * 4,
    ],
)
def test_prescriptive_phrasings_are_accepted(phrasing: str) -> None:
    assert "no_trigger_phrase" not in T.lint_tool(tool("t", phrasing))


def test_short_and_absent_descriptions_are_distinguished() -> None:
    assert T.lint_tool(tool("t", "Too terse.")) == ["short_description", "no_trigger_phrase"]
    assert T.lint_tool(tool("t", "")) == ["no_description"]


def test_an_empty_description_is_not_also_reported_as_short_or_untriggered() -> None:
    """One finding per problem: 'no_description' already says everything."""
    assert T.lint_tool(tool("t", "")) == ["no_description"]


def test_similarity_is_symmetric_and_ignores_filler_words() -> None:
    a = "Search for a product by name in the catalogue"
    b = "Search the catalogue for a product by its name"
    assert T.similarity(a, b) == T.similarity(b, a) == 1.0


def test_similarity_of_unrelated_or_empty_descriptions() -> None:
    assert T.similarity("Upload an image file", "Rank bestselling products") == 0.0
    assert T.similarity("", "") == 0.0
    assert T.similarity(None, "anything") == 0.0


def test_uniform_properties_are_catalogue_facts_not_per_tool_findings() -> None:
    """The regression this shape exists to avoid.

    Every parameter in the real catalogue lacks a schema description and every
    string parameter is unconstrained. As per-tool findings that is 30 rows all
    reporting one decision about how the server is written; as two counted facts
    it is informative.
    """
    schema: JsonObject = {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    report = T.lint(catalogue(tool("one", schema=schema), tool("two", schema=schema)))

    assert report["facts"]["params_undocumented"] == 4
    assert report["facts"]["string_params_unconstrained"] == 4
    for entry in report["tools"].values():
        assert entry["findings"] == []


def test_constrained_and_documented_params_are_counted_as_such() -> None:
    schema: JsonObject = {
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"], "description": "which mode"},
            "when": {"type": "string", "format": "date"},
            "who": {"type": "string", "pattern": "^x"},
            "count": {"type": "integer"},
        }
    }
    facts = T.lint(catalogue(tool("t", schema=schema)))["facts"]

    assert facts["params"] == 4
    assert facts["params_undocumented"] == 3
    assert facts["string_params"] == 3
    assert facts["string_params_unconstrained"] == 0


def test_malformed_property_specs_do_not_crash_the_count() -> None:
    facts = T.lint(catalogue(tool("t", schema={"properties": {"a": "not a dict", "b": {"type": "string"}}})))["facts"]
    assert facts["params"] == 1


def test_similarity_never_becomes_a_per_tool_finding() -> None:
    """Measured against confirmed confusions it is a weak predictor, so it must
    not appear where a reader would take it for a defect."""
    report = T.lint(catalogue(tool("one"), tool("two")))

    assert report["tools"]["one"]["findings"] == []
    assert report["similar_pairs"][0]["similarity"] == 1.0


def test_similar_pairs_are_ranked_and_capped() -> None:
    tools = [tool(f"t{i}", f"Shared wording about carts and orders number {i}") for i in range(6)]
    assert len(T.similar_pairs(tools, limit=3)) == 3
    scores = [p["similarity"] for p in T.similar_pairs(tools, limit=3)]
    assert scores == sorted(scores, reverse=True)


def test_pairs_with_nothing_in_common_are_omitted_entirely() -> None:
    pairs = T.similar_pairs([tool("a", "Upload image files"), tool("b", "Rank bestselling products")])
    assert pairs == []


def test_tools_without_a_name_are_skipped() -> None:
    report = T.lint(catalogue(cast(ToolDef, cast(object, {"description": "nameless"})), tool("real")))
    assert list(report["tools"]) == ["real"]


def test_render_states_the_advertising_cost_and_the_caveat() -> None:
    out = T.render(T.lint(catalogue(tool("a", "Too terse."), tool("b", "Too terse also."))))

    assert "Tool catalogue lint" in out
    assert "tokens to advertise" in out
    assert "short_description" in out
    assert "Use it to explain a confirmed collision, not to predict one." in out


def test_render_says_so_when_nothing_is_flagged() -> None:
    assert "No per-tool findings." in T.render(T.lint(catalogue(tool("clean"))))


def test_main_is_advisory_and_never_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A build that goes red over prose style is one people learn to bypass."""
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"tools": [tool("a", "Too terse.")]}))
    monkeypatch.setattr("sys.argv", ["toollint", "--snapshot", str(snap)])

    assert T.main() == 0
    assert "short_description" in capsys.readouterr().out


def test_main_reports_an_unreadable_snapshot_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["toollint", "--snapshot", str(tmp_path / "absent.json")])

    assert T.main() == 1
    assert "::error::" in capsys.readouterr().err


def test_the_committed_catalogue_lints_cleanly_enough_to_be_useful() -> None:
    """Guards the checks against becoming vacuous or universal.

    A check that fires on nothing is dead weight; one that fires on everything
    is the noise this module was trimmed to avoid.
    """
    path = Path(__file__).resolve().parents[1] / "tool-history" / "latest.json"
    snapshot = cast(Snapshot, cast(object, as_object(cast(object, json.loads(path.read_text())))))
    report = T.lint(snapshot)
    flagged = [n for n, t in report["tools"].items() if t["findings"]]

    assert 0 < len(flagged) < len(report["tools"])
