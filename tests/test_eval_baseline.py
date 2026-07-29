"""Baseline mode, the provider adapters, and the per-fixture result records.

Baseline is the v1 reference the whole discovery comparison is measured against:
the full catalogue in one request, grading the first tool call. It reached the
model on every run but was never covered, because doing so needs a fake provider
client — which is all these are.

The result records matter as much as the flow: `skipped_result` and
`error_result` are what keep an absent plugin and a server 500 out of the
failure count, and their shape is what eval/scoring.py filters on.
"""

import pytest

from eval import runner as E

TOOL = "shopware-entity-read"


def fixture(fid="f1", tool=TOOL, **over):
    return {"id": fid, "prompt": "Read the product.", "expected_tool": tool, "category": "unambiguous", **over}


class FakeClient:
    """Records what it was asked and replies with a scripted tool call."""

    def __init__(self, tool_name=TOOL, *, raises=None, tokens=(120, 8)):
        self.tool_name = tool_name
        self.raises = raises
        self.tokens = tokens
        self.seen = []


def fake_turn(client, model, system_prompt, messages, tools):
    if client.raises:
        raise client.raises
    client.seen.append({"model": model, "system": system_prompt, "messages": messages, "tools": tools})
    calls = [{"id": "c1", "name": client.tool_name, "input": {"entity": "product"}}] if client.tool_name else []
    return {
        "tool_calls": calls,
        "assistant_message": {"role": "assistant", "content": None},
        "tool_result_builder": lambda r: [],
        "stop_reason": "tool_calls" if calls else "stop",
        "tokens": {"input": client.tokens[0], "output": client.tokens[1]},
    }


@pytest.fixture(autouse=True)
def stub_turns(monkeypatch):
    monkeypatch.setattr(E, "anthropic_turn", fake_turn)
    monkeypatch.setattr(E, "openai_turn", fake_turn)


# ---------------------------------------------------------------------------
# Provider tool-schema adapters
# ---------------------------------------------------------------------------
def test_anthropic_adapter_uses_input_schema():
    out = E.tools_for_anthropic([{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])

    assert out == [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]


def test_openai_adapter_nests_under_function():
    out = E.tools_for_openai([{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])

    assert out == [
        {"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}
    ]


@pytest.mark.parametrize("adapter,key", [(E.tools_for_anthropic, "input_schema"), (E.tools_for_openai, None)])
def test_a_tool_without_a_schema_gets_an_empty_object_not_null(adapter, key):
    """OpenAI rejects a null or list-typed `properties`, so the default has to be
    a real empty object."""
    out = adapter([{"name": "t"}])
    schema = out[0][key] if key else out[0]["function"]["parameters"]

    assert schema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# run_fixture_baseline
# ---------------------------------------------------------------------------
def test_baseline_grades_the_first_tool_call():
    result = run = E.run_fixture_baseline("openai", FakeClient(), [], fixture(), "m", None)

    assert run["selected_tool"] == TOOL
    assert result["passed"] is True
    assert result["mode"] == "baseline" and result["steps"] == 1


def test_baseline_marks_a_wrong_tool_as_failed():
    result = E.run_fixture_baseline("openai", FakeClient("other-tool"), [], fixture(), "m", None)

    assert result["selected_tool"] == "other-tool" and result["passed"] is False


def test_baseline_records_no_selection_when_the_model_called_nothing():
    result = E.run_fixture_baseline("openai", FakeClient(None), [], fixture(), "m", None)

    assert result["selected_tool"] is None and result["selected_input"] == {}
    assert result["passed"] is False
    assert result["stop_reason"] == "stop"


def test_baseline_accepts_an_alternative_from_acceptable_tools():
    result = E.run_fixture_baseline("openai", FakeClient("alt"), [], fixture(acceptable_tools=["alt"]), "m", None)

    assert result["passed"] is True


def test_openai_carries_the_system_prompt_in_the_messages():
    """The two providers differ here: OpenAI takes it as a message, Anthropic as
    a top-level parameter, and sending it the wrong way silently drops it."""
    client = FakeClient()

    E.run_fixture_baseline("openai", client, [], fixture(), "m", "SYSTEM")

    assert client.seen[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert client.seen[0]["system"] is None


def test_anthropic_carries_the_system_prompt_as_a_parameter():
    client = FakeClient()

    E.run_fixture_baseline("anthropic", client, [], fixture(), "m", "SYSTEM")

    assert client.seen[0]["system"] == "SYSTEM"
    assert [m["role"] for m in client.seen[0]["messages"]] == ["user"]


def test_baseline_reports_tokens_and_latency():
    result = E.run_fixture_baseline("openai", FakeClient(tokens=(500, 25)), [], fixture(), "m", None)

    assert result["tokens"] == {"input": 500, "output": 25}
    assert result["latency_s"] >= 0


def test_baseline_carries_the_fixture_note_into_the_record():
    result = E.run_fixture_baseline("openai", FakeClient(), [], fixture(notes="why this matters"), "m", None)

    assert result["notes"] == "why this matters"


# ---------------------------------------------------------------------------
# Result records for fixtures that never ran
# ---------------------------------------------------------------------------
def test_skipped_result_is_excluded_from_scoring_not_counted_as_a_failure():
    r = E.skipped_result(fixture(), "baseline")

    assert r["skipped"] is True and r["passed"] is False
    assert "not registered on this instance" in r["skip_reason"]
    assert E.scored([r]) == []


def test_error_result_is_graded_but_not_gating():
    r = E.error_result(fixture(), "baseline", RuntimeError("500 Server Error"))

    assert r["error"] == "500 Server Error" and r["passed"] is False
    assert len(E.scored([r])) == 1, "it counts against the error budget"
    assert E.executed([r]) == [], "but it must not count against the pass rate"


def test_a_discovery_error_record_carries_the_discovery_fields():
    """discovery_summary indexes steps and discovery_path directly, so an errored
    discovery fixture without them would crash the summary."""
    r = E.error_result(fixture(), "discovery", RuntimeError("boom"))

    assert r["steps"] == 0 and r["discovery_path"] == "none"
    assert r["search_hit"] is None and r["enabled_correct_toolset"] is None


def test_a_baseline_error_record_omits_the_discovery_fields():
    assert "discovery_path" not in E.error_result(fixture(), "baseline", RuntimeError("boom"))


# ---------------------------------------------------------------------------
# run_baseline_pass — skips, errors and concurrency
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_mcp(monkeypatch):
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: ("sid", ""))
    monkeypatch.setattr(E, "enable_all_toolsets", lambda _s, endpoint=None: [])
    monkeypatch.setattr(
        E, "mcp_tools_list_all", lambda _s, endpoint=None: [{"name": TOOL, "description": "d", "inputSchema": {}}]
    )


def test_baseline_pass_runs_every_fixture(stub_mcp, capsys):
    results = E.run_baseline_pass("openai", FakeClient(), [fixture("a"), fixture("b")], "m", None, {TOOL}, workers=1)
    capsys.readouterr()

    assert [r["id"] for r in results] == ["a", "b"]
    assert all(r["passed"] for r in results)


def test_baseline_pass_skips_a_fixture_whose_tool_is_absent(stub_mcp, capsys):
    """An instance without the dev-tools bundle must not fail its fixtures."""
    results = E.run_baseline_pass(
        "openai", FakeClient(), [fixture("a"), fixture("d", tool="swag-dev-tools-log-search")], "m", None, {TOOL}
    )
    capsys.readouterr()

    assert [r["id"] for r in results if r.get("skipped")] == ["d"]


def test_baseline_pass_records_a_raising_fixture_as_an_error_and_keeps_going(stub_mcp, capsys):
    results = E.run_baseline_pass(
        "openai", FakeClient(raises=RuntimeError("429 rate limit")), [fixture("a")], "m", None, {TOOL}
    )
    capsys.readouterr()

    assert results[0]["error"] == "429 rate limit"


def test_baseline_pass_preserves_fixture_order_when_run_concurrently(stub_mcp, capsys):
    """Results are written back by index, so a fast fixture finishing first must
    not reorder the report."""
    fixtures = [fixture(f"f{i}") for i in range(8)]

    results = E.run_baseline_pass("openai", FakeClient(), fixtures, "m", None, {TOOL}, workers=4)
    capsys.readouterr()

    assert [r["id"] for r in results] == [f"f{i}" for i in range(8)]


def test_baseline_pass_attaches_a_progress_line_to_each_result(stub_mcp, capsys):
    results = E.run_baseline_pass("openai", FakeClient(), [fixture("a")], "m", None, {TOOL})
    capsys.readouterr()

    assert "_line" in results[0]


# ---------------------------------------------------------------------------
# tool-search payload parsing
# ---------------------------------------------------------------------------
def test_search_results_extract_the_inline_tool_definitions():
    text = '{"data": [{"tool": {"name": "a", "description": "d"}}, {"tool": {"name": "b"}}]}'

    assert [t["name"] for t in E._search_result_tools(text)] == ["a", "b"]


def test_search_results_of_a_non_json_body_are_empty():
    assert E._search_result_tools("not json") == []


def test_search_results_tolerate_entries_without_a_tool():
    text = '{"data": [{"score": 1}, {"tool": null}, {"tool": {"name": "ok"}}, "bare"]}'

    assert [t["name"] for t in E._search_result_tools(text)] == ["ok"]


def test_search_results_drop_a_tool_with_no_name():
    """A nameless tool cannot be called or matched, so it is not a result."""
    assert E._search_result_tools('{"data": [{"tool": {"description": "d"}}]}') == []


def test_search_results_of_an_empty_payload_are_empty():
    assert E._search_result_tools("{}") == []


def test_search_contains_expected_matches_by_name():
    text = '{"data": [{"tool": {"name": "wanted"}}]}'

    assert E._search_contains_expected(text, "wanted") is True
    assert E._search_contains_expected(text, "other") is False
