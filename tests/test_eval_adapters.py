"""The provider adapters, system-prompt routing, and the per-fixture result records.

These need a fake provider client, which is what most of this file is.

This was `test_eval_baseline.py`, covering the removed baseline mode alongside
these. The provider-specific system-prompt tests now run against
`run_fixture_discovery` instead, which is the live path and had no direct test of
its own; the two providers differ there (OpenAI takes the prompt as a message,
Anthropic as a top-level parameter) and sending it the wrong way silently drops
it.

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


@pytest.fixture
def stub_mcp(monkeypatch):
    """Discovery opens a session and reads the advertised surface before its first
    turn, so both have to answer for the fixture to reach the fake client."""
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: ("sid", ""))
    monkeypatch.setattr(
        E, "mcp_tools_list_all", lambda _s, endpoint=None: [{"name": TOOL, "description": "d", "inputSchema": {}}]
    )


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
# System-prompt routing, which differs per provider
# ---------------------------------------------------------------------------
def test_openai_carries_the_system_prompt_in_the_messages(stub_mcp):
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(), "m", "SYSTEM", 6)

    assert client.seen[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert client.seen[0]["system"] is None


def test_anthropic_carries_the_system_prompt_as_a_parameter(stub_mcp):
    client = FakeClient()

    E.run_fixture_discovery("anthropic", client, fixture(), "m", "SYSTEM", 6)

    assert client.seen[0]["system"] == "SYSTEM"
    # The agentic loop appends to the same list it handed over, so assert the
    # invariant rather than the exact contents: Anthropic must get no system
    # message, because it already has the prompt as a parameter.
    assert client.seen[0]["messages"][0]["role"] == "user"
    assert not [m for m in client.seen[0]["messages"] if m["role"] == "system"]


def test_a_fixture_reaching_its_expected_tool_passes(stub_mcp):
    """The happy path through run_fixture_discovery: the first non-meta call is
    terminal and graded, and it is not executed."""
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL and result["passed"] is True
    assert result["mode"] == "discovery"
    assert result["tokens"] == {"input": 120, "output": 8}
    assert result["latency_s"] >= 0


def test_a_wrong_tool_is_recorded_with_what_was_picked(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient("other-tool"), fixture(), "m", None, 6)

    assert result["selected_tool"] == "other-tool" and result["passed"] is False
    assert result["fail_reason"] == "wrong_tool"


def test_an_alternative_from_acceptable_tools_is_accepted(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient("alt"), fixture(acceptable_tools=["alt"]), "m", None, 6)

    assert result["passed"] is True


def test_no_tool_call_at_all_is_its_own_fail_reason(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient(None), fixture(), "m", None, 6)

    assert result["selected_tool"] is None and result["passed"] is False
    assert result["fail_reason"] == "no_tool_call"


def test_the_fixture_note_is_carried_into_the_record(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(notes="why this matters"), "m", None, 6)

    assert result["notes"] == "why this matters"


# ---------------------------------------------------------------------------
# Result records for fixtures that never ran
# ---------------------------------------------------------------------------
def test_skipped_result_is_excluded_from_scoring_not_counted_as_a_failure():
    r = E.skipped_result(fixture(), "discovery")

    assert r["skipped"] is True and r["passed"] is False
    assert "not registered on this instance" in r["skip_reason"]
    assert E.scored([r]) == []


def test_error_result_is_graded_but_not_gating():
    r = E.error_result(fixture(), "discovery", RuntimeError("500 Server Error"))

    assert r["error"] == "500 Server Error" and r["passed"] is False
    assert len(E.scored([r])) == 1, "it counts against the error budget"
    assert E.executed([r]) == [], "but it must not count against the pass rate"


def test_a_discovery_error_record_carries_the_discovery_fields():
    """discovery_summary indexes steps and discovery_path directly, so an errored
    discovery fixture without them would crash the summary."""
    r = E.error_result(fixture(), "discovery", RuntimeError("boom"))

    assert r["steps"] == 0 and r["discovery_path"] == "none"
    assert r["search_hit"] is None and r["enabled_correct_toolset"] is None


def test_an_error_record_for_another_mode_omits_the_discovery_fields():
    """Discovery is the only mode the runner has, but the helper stays generic, so
    a caller passing anything else must not get half-populated discovery keys."""
    assert "discovery_path" not in E.error_result(fixture(), "other", RuntimeError("boom"))


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
