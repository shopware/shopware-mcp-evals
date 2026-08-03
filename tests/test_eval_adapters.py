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

import json

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


def dispatch_turn(client, model, system_prompt, messages, tools):
    """Route to whichever fake the client is: FakeClient repeats one call,
    ScriptedClient plays a sequence. One dispatcher so no test has to remember
    to swap the turn function in as well as the client."""
    if hasattr(client, "script"):
        return scripted_turn(client, model, system_prompt, messages, tools)
    return fake_turn(client, model, system_prompt, messages, tools)


@pytest.fixture(autouse=True)
def stub_turns(monkeypatch):
    monkeypatch.setattr(E, "anthropic_turn", dispatch_turn)
    monkeypatch.setattr(E, "openai_turn", dispatch_turn)


@pytest.fixture
def stub_mcp(monkeypatch):
    """Discovery opens a session and reads the advertised surface before its first
    turn, so both have to answer for the fixture to reach the fake client.

    `mcp_call` is stubbed too because the answering call is now executed, not
    just graded — an unstubbed one would reach the network.
    """
    monkeypatch.setattr(E, "mcp_init", lambda endpoint=None: ("sid", ""))
    monkeypatch.setattr(
        E, "mcp_tools_list_all", lambda _s, endpoint=None: [{"name": TOOL, "description": "d", "inputSchema": {}}]
    )
    monkeypatch.setattr(E, "mcp_call", lambda *a, **k: {"_reply": '{"data": [{"id": "x"}]}'})
    monkeypatch.setattr(E, "mcp_call_error", lambda resp: None)
    monkeypatch.setattr(E, "mcp_result_text", lambda resp: resp["_reply"])


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
    assert result["tokens"] == {"input": 120, "cached_input": 0, "output": 8}
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


def test_search_rows_carry_rank_score_and_pool_size():
    """The server ranks and scores; the runner used to keep neither.

    Rank is the point: `search_hit` cannot tell first place from ninth, and
    that difference decides whether a model reading a 20-result list ever
    reaches the tool.
    """
    text = json.dumps(
        {
            "data": [
                {"tool": {"name": "first"}, "score": 9.5, "matchedIn": "name"},
                {"tool": {"name": "second"}, "score": 4.0, "matchedIn": "description"},
            ],
            "_meta": {"query": "q", "totalCandidates": 27},
        }
    )

    rows, candidates = E._search_rows(text)

    assert [(r["tool"]["name"], r["rank"]) for r in rows] == [("first", 1), ("second", 2)]
    assert rows[0]["score"] == 9.5
    assert rows[1]["matched_in"] == "description"
    assert candidates == 27


def test_search_rows_rank_by_server_order_not_by_score():
    """Ranking is the server's job. Re-sorting here would measure our idea of
    relevance instead of the one the model was actually shown."""
    text = json.dumps({"data": [{"tool": {"name": "low"}, "score": 1}, {"tool": {"name": "high"}, "score": 99}]})

    rows, _ = E._search_rows(text)

    assert [r["tool"]["name"] for r in rows] == ["low", "high"]
    assert [r["rank"] for r in rows] == [1, 2]


def test_search_rows_skip_unusable_entries_without_shifting_later_ranks():
    """Rank is the position the server returned, so a dropped nameless entry
    must not renumber the rows after it — that would report a tool as ranking
    better than it did."""
    text = json.dumps({"data": [{"tool": {"description": "nameless"}}, {"tool": {"name": "real"}}]})

    rows, _ = E._search_rows(text)

    assert [(r["tool"]["name"], r["rank"]) for r in rows] == [("real", 2)]


def test_search_rows_of_an_unparseable_or_empty_body():
    assert E._search_rows("not json") == ([], None)
    assert E._search_rows("{}") == ([], None)
    assert E._search_rows('{"data": [], "_meta": "not a dict"}') == ([], None)


# ---------------------------------------------------------------------------
# Search rank recorded across a whole fixture run
# ---------------------------------------------------------------------------
class ScriptedClient:
    """Plays one scripted tool call per turn."""

    def __init__(self, script):
        self.script = list(script)
        self.raises = None
        self.tokens = (10, 1)
        self.seen = []


def scripted_turn(client, model, system_prompt, messages, tools):
    """Plays the next scripted call, then answers in prose once out of script —
    which is how a model that has decided to decline actually behaves."""
    if not client.script:
        return {
            "tool_calls": [],
            "assistant_message": {"role": "assistant", "content": "Nothing here can do that."},
            "tool_result_builder": lambda r: [],
            "stop_reason": "stop",
            "tokens": {"input": 5, "output": 5},
        }
    name, payload = client.script.pop(0)
    return {
        "tool_calls": [{"id": f"c{len(client.seen)}", "name": name, "input": payload}],
        "assistant_message": {"role": "assistant", "content": None},
        "tool_result_builder": lambda r: [],
        "stop_reason": "tool_calls",
        "tokens": {"input": 10, "output": 1},
    }


def search_payload(names, *, candidates=27, scores=None):
    scores = scores or {}
    return json.dumps(
        {
            "data": [{"tool": {"name": n, "description": "d"}, "score": scores.get(n, 1.0)} for n in names],
            "_meta": {"totalCandidates": candidates},
        }
    )


@pytest.fixture
def stub_search(monkeypatch, stub_mcp):
    """Feed scripted tool-search payloads back as if the server answered."""
    replies = []
    monkeypatch.setattr(E, "mcp_call", lambda *a, **k: {"_reply": replies.pop(0) if replies else "{}"})
    monkeypatch.setattr(E, "mcp_call_error", lambda resp: None)
    monkeypatch.setattr(E, "mcp_result_text", lambda resp: resp["_reply"])
    return replies


def test_best_rank_across_several_searches_is_the_one_recorded(stub_search):
    """A fixture may search more than once. The best placement the expected tool
    reached is what the model had its best chance from, so a later, vaguer query
    must not make the catalogue look worse than it is."""
    stub_search.append(search_payload(["a", "b", "c", "d", TOOL]))
    stub_search.append(search_payload(["x", TOOL], scores={TOOL: 8.5}))

    result = E.run_fixture_discovery(
        "openai",
        ScriptedClient(
            [
                ("shopware-tool-search", {"query": "vague"}),
                ("shopware-tool-search", {"query": "precise"}),
                (TOOL, {"entity": "product"}),
            ]
        ),
        fixture(),
        "m",
        None,
        6,
    )

    assert result["search_rank"] == 2
    assert result["search_score"] == 8.5
    assert result["search_candidates"] == 27
    assert result["search_hit"] is True
    assert result["passed"] is True


def test_a_search_that_never_surfaces_the_tool_has_no_rank(stub_search):
    """search_hit False and search_rank None must agree — a rank of 0 or a
    sentinel here would be averaged into the ranking stats as a good placement."""
    stub_search.append(search_payload(["unrelated"]))

    result = E.run_fixture_discovery(
        "openai",
        ScriptedClient([("shopware-tool-search", {"query": "q"}), (TOOL, {})]),
        fixture(),
        "m",
        None,
        6,
    )

    assert result["search_hit"] is False
    assert result["search_rank"] is None
    assert result["search_score"] is None


def test_a_fixture_that_never_searches_records_no_rank(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result["search_rank"] is None
    assert result["search_candidates"] is None


# ---------------------------------------------------------------------------
# Negative fixtures end to end
# ---------------------------------------------------------------------------
def negative_fixture(**over):
    return {
        "id": "neg1",
        "category": "negative",
        "prompt": "Publish this to our Instagram account.",
        "expect_no_tool": True,
        **over,
    }


def test_a_negative_fixture_passes_when_the_model_answers_in_prose(stub_mcp):
    """No tool call is the pass, and it must not carry a failure reason."""
    result = E.run_fixture_discovery("openai", FakeClient(None), negative_fixture(), "m", None, 6)

    assert result["passed"] is True
    assert result["selected_tool"] is None
    assert result["fail_reason"] is None
    assert result["expected_tool"] is None


def test_a_negative_fixture_fails_when_any_tool_is_reached_for(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient(TOOL), negative_fixture(), "m", None, 6)

    assert result["passed"] is False
    assert result["selected_tool"] == TOOL


def test_a_negative_fixture_fails_on_the_step_cap(stub_search):
    """Still rummaging when the budget ran out is not restraint."""
    stub_search.extend([search_payload(["unrelated"])] * 3)

    result = E.run_fixture_discovery(
        "openai",
        ScriptedClient([("shopware-tool-search", {"query": f"q{i}"}) for i in range(3)]),
        negative_fixture(),
        "m",
        None,
        3,
    )

    assert result["fail_reason"] == "step_cap"
    assert result["passed"] is False


def test_a_negative_fixture_may_search_first_and_still_pass(stub_search):
    """Searching, finding nothing, then declining is the behaviour under test —
    meta calls are free."""
    stub_search.append(search_payload(["unrelated-tool"]))

    result = E.run_fixture_discovery(
        "openai",
        ScriptedClient([("shopware-tool-search", {"query": "instagram"})]),
        negative_fixture(),
        "m",
        None,
        6,
    )

    assert result["passed"] is True
    assert result["discovery_path"] == "none"
    assert [m["tool"] for m in result["meta_calls"]] == ["shopware-tool-search"]


def test_payload_bytes_and_surface_tokens_are_recorded(stub_search):
    """Surface at turn one is the price of v2's promise; the peak is what the
    model actually paid once discovery pulled tools in."""
    stub_search.append(search_payload(["found-tool"]))

    result = E.run_fixture_discovery(
        "openai",
        ScriptedClient([("shopware-tool-search", {"query": "q"}), (TOOL, {})]),
        fixture(),
        "m",
        None,
        6,
    )

    # Every tool result the model was made to read, the answering call included
    # — that call's payload is exactly the cost a real client would pay.
    search_bytes = len(search_payload(["found-tool"]).encode("utf-8"))
    assert result["payload_bytes"] > search_bytes
    assert result["surface_tokens"] > 0
    assert result["surface_tokens_peak"] > result["surface_tokens"], "search added a tool to the surface"


# ---------------------------------------------------------------------------
# Execution, assertions and recovery
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_exec(monkeypatch, stub_mcp):
    """Scripted server replies for the answering call, keyed by tool name."""
    replies = {}

    def call(_sid, name, args, endpoint=None):
        return {"_reply": replies.get(name, '{"data": [{"id": "x"}]}'), "_args": args, "_name": name}

    calls = []
    monkeypatch.setattr(E, "mcp_call", lambda s, n, a, endpoint=None: (calls.append((n, a)), call(s, n, a))[1])
    monkeypatch.setattr(E, "mcp_call_error", lambda resp: resp.get("_err"))
    monkeypatch.setattr(E, "mcp_result_text", lambda resp: resp["_reply"])
    return replies, calls


def test_the_answering_call_is_executed_not_just_graded(stub_exec):
    """The gap this closes: a correctly named tool with nonsense arguments used
    to score the same as one that runs."""
    _, calls = stub_exec

    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert (TOOL, {"entity": "product"}) in calls
    assert result["execution"] == "executed"
    assert result["passed"] is True
    assert result["first_try"] is True


def test_a_correctly_named_call_the_server_rejects_now_fails(stub_exec, monkeypatch):
    monkeypatch.setattr(E, "mcp_call", lambda *a, **k: {"_reply": "", "_err": "Validation failed: entity is required"})

    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL, "it named the right tool"
    assert result["passed"] is False, "but the call did not work"
    assert result["fail_reason"] == "invalid_arguments"


def test_a_wrong_first_pick_can_be_recovered_from(stub_exec):
    """The signal the suite could not previously produce: picking wrong and then
    correcting is materially different from never getting there."""
    client = ScriptedClient([("shopware-entity-schema", {}), (TOOL, {"entity": "product"})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert result["passed"] is True
    assert result["first_try"] is False
    assert result["recovered"] is True
    assert result["first_tool_correct"] is False
    assert result["wrong_calls"] == 1
    assert [a["tool"] for a in result["attempted_tools"]] == ["shopware-entity-schema", TOOL]
    assert result["steps_to_correct"] == 2


def test_selected_tool_stays_the_first_answer_after_a_recovery(stub_exec):
    """`selected_tool` keeps its old meaning so historical reports and the
    per-tool scorecard still compare like with like."""
    client = ScriptedClient([("shopware-entity-schema", {}), (TOOL, {})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert result["selected_tool"] == "shopware-entity-schema"
    assert result["passed"] is True


def test_flailing_until_the_step_cap_is_not_a_recovery(stub_exec):
    client = ScriptedClient([("shopware-entity-schema", {}), ("shopware-entity-aggregate", {})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 2)

    assert result["passed"] is False
    assert result["recovered"] is False
    assert result["wrong_calls"] == 2


def test_a_mutating_tool_is_executed_with_dry_run_forced_on(stub_exec):
    _, calls = stub_exec

    result = E.run_fixture_discovery(
        "openai",
        FakeClient("shopware-entity-delete"),
        fixture(tool="shopware-entity-delete"),
        "m",
        None,
        6,
    )

    assert calls[-1][1]["dryRun"] is True
    assert result["dry_run_forced"] is True
    assert result["passed"] is True


def test_an_unsafe_tool_is_graded_on_selection_and_never_called(stub_exec):
    """media-upload, cart-manage and scaffold mutate with no dryRun, so they
    keep the old selection-only grading rather than being executed."""
    _, calls = stub_exec

    result = E.run_fixture_discovery(
        "openai", FakeClient("shopware-media-upload"), fixture(tool="shopware-media-upload"), "m", None, 6
    )

    assert calls == [], "nothing was sent to the server"
    assert result["execution"] == "skipped_unsafe"
    assert result["passed"] is True, "graded on selection, as before"
    assert result["attempted_tools"][0]["executed"] is False


def test_an_unknown_tool_is_not_executed_either(stub_exec):
    _, calls = stub_exec

    result = E.run_fixture_discovery("openai", FakeClient("tool-shipped-yesterday"), fixture(), "m", None, 6)

    assert calls == []
    assert result["execution"] == "skipped_unclassified"
    assert result["passed"] is False


def test_a_negative_fixture_that_calls_a_tool_does_not_get_to_recover(stub_exec):
    """Calling anything IS the failure, so there is nothing to recover from."""
    result = E.run_fixture_discovery("openai", FakeClient(TOOL), negative_fixture(), "m", None, 6)

    assert result["passed"] is False
    assert result["selected_tool"] == TOOL


def test_a_data_tier_fixture_fails_on_an_empty_result(stub_exec):
    replies, _ = stub_exec
    replies[TOOL] = '{"data": []}'

    result = E.run_fixture_discovery(
        "openai",
        FakeClient(),
        fixture(expect_result={"tier": "data", "min_items": {"path": "data", "n": 1}}),
        "m",
        None,
        6,
    )

    assert result["passed"] is False
    assert result["fail_reason"] == "too_few:data<1"


# ---------------------------------------------------------------------------
# Diagnostic arms
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_arms(monkeypatch, stub_exec):
    """Record which toolsets an arm enabled before the model saw anything."""
    enabled = {"one": [], "all": 0}
    monkeypatch.setattr(E, "enable_toolset", lambda _s, ts, endpoint=None: enabled["one"].append(ts))
    monkeypatch.setattr(
        E, "enable_all_toolsets", lambda _s, endpoint=None: enabled.__setitem__("all", enabled["all"] + 1)
    )
    monkeypatch.setattr(
        E,
        "mcp_tools_list_all",
        lambda _s, endpoint=None: [
            {"name": TOOL, "description": "d", "inputSchema": {}},
            {"name": "shopware-tool-search", "description": "d", "inputSchema": {}},
        ],
    )
    return enabled


def test_the_isolated_arm_pre_enables_only_the_fixtures_own_group(stub_arms):
    E.run_fixture_discovery("openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="isolated")

    assert stub_arms["one"] == ["entity"]
    assert stub_arms["all"] == 0


def test_the_full_arm_enables_everything(stub_arms):
    E.run_fixture_discovery("openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="full")

    assert stub_arms["all"] == 1
    assert stub_arms["one"] == []


@pytest.mark.parametrize("arm", ["isolated", "full"])
def test_the_diagnostic_arms_withhold_the_meta_tools(stub_arms, arm):
    """The fix for the bug that killed `baseline`: with no meta-tool advertised
    there is no meta-call to misgrade as a wrong answer."""
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(expected_toolset="entity"), "m", None, 6, arm=arm)

    offered = {t["function"]["name"] for t in client.seen[0]["tools"]}
    assert "shopware-tool-search" not in offered
    assert TOOL in offered


def test_the_discovery_arm_still_offers_them(stub_arms):
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert "shopware-tool-search" in {t["function"]["name"] for t in client.seen[0]["tools"]}


def test_the_arm_is_recorded_on_the_record(stub_arms):
    result = E.run_fixture_discovery(
        "openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="full"
    )
    assert result["mode"] == "full"


# ---------------------------------------------------------------------------
# Triage: only the failures, and only the categories the arms can speak to
# ---------------------------------------------------------------------------
def triage(discovery, fixtures, **kw):
    return E.triage_arms("openai", FakeClient(), discovery, fixtures, "m", None, 6, **kw)


def disc(fid, passed, **extra):
    return {"id": fid, "passed": passed, **extra}


def test_only_discovery_failures_are_re_run(stub_arms, capsys):
    fixtures = [fixture("won", expected_toolset="entity"), fixture("lost", expected_toolset="entity")]

    out = triage([disc("won", True), disc("lost", False)], fixtures)
    capsys.readouterr()

    assert [r["id"] for r in out["isolated"]] == ["lost"]
    assert [r["id"] for r in out["full"]] == ["lost"]


def test_a_clean_run_triages_nothing(stub_arms, capsys):
    out = triage([disc("won", True)], [fixture("won", expected_toolset="entity")])
    assert out == {}
    assert "no discovery failures" in capsys.readouterr().out


def test_errored_fixtures_are_not_triaged(stub_arms, capsys):
    """A 500 is missing data, not a description problem — re-running it under
    two more arms just spends money on the same 500."""
    out = triage([disc("boom", False, error="500")], [fixture("boom", expected_toolset="entity")])
    capsys.readouterr()
    assert out == {}


@pytest.mark.parametrize("category", ["meta", "discovery", "negative"])
def test_categories_the_arms_cannot_speak_to_are_skipped(stub_arms, capsys, category):
    """meta wants a withheld tool, discovery exists to exercise the layer the
    arms bypass, and pre-enabling a group to ask 'does anything bite' is a
    different question."""
    fixtures = [fixture("f", category=category, expected_toolset="entity")]

    out = triage([disc("f", False)], fixtures)
    capsys.readouterr()

    assert out == {}


def test_an_arm_that_cannot_advertise_the_tool_reports_setup_failure(stub_arms):
    """An arm that never put the tool in front of the model answers nothing
    about its description — grading it would be inventing a finding."""
    result = E.run_fixture_discovery(
        "openai",
        FakeClient(),
        fixture(tool="shopware-entity-delete", expected_toolset="nonexistent"),
        "m",
        None,
        6,
        arm="isolated",
    )

    assert result["skipped"] is True
    assert "arm setup failed" in result["skip_reason"]
    assert "nonexistent" in result["skip_reason"]


def test_the_discovery_arm_never_reports_setup_failure(stub_mcp):
    """Discovery starts from the default surface by design — the tool being
    absent is the thing it measures, not a broken experiment."""
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert not result.get("skipped")


def test_a_failed_call_keeps_its_reason_when_the_model_then_gives_up(stub_exec, monkeypatch):
    """The bug that hid a broken assertion tier for three runs.

    The model called the right tool, the call failed, and it answered in prose
    on the next turn — so `no_tool_call` overwrote the real reason and every
    report said the model had chosen nothing.
    """
    monkeypatch.setattr(E, "mcp_call", lambda *a, **k: {"_reply": "", "_err": "Missing required parameter: id"})

    result = E.run_fixture_discovery("openai", ScriptedClient([(TOOL, {})]), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL, "it did choose a tool"
    assert result["fail_reason"] == "invalid_arguments", "and the informative reason survives"
    assert result["attempted_tools"][0]["error"].startswith("Missing required parameter")


def test_an_in_band_failure_records_what_the_server_said(stub_exec):
    """The admin merchant/entity tools answer a rejected call with `isError:
    false` and `{"success": false, ...}` in the body, so `mcp_call_error` is
    empty and only the in-band message exists. Recording just the transport
    error is why every failed attempt in the last CI run read `tool_error` with
    an empty `error` — the five gating failures had to be diagnosed from the
    fixture text rather than from what the server actually replied.
    """
    replies, _ = stub_exec
    replies[TOOL] = '{"success": false, "error": {"type": "internal", "message": "Cart is empty."}}'

    result = E.run_fixture_discovery("openai", ScriptedClient([(TOOL, {})]), fixture(), "m", None, 6)

    assert result["attempted_tools"][0]["reason"] == "tool_error"
    assert result["attempted_tools"][0]["error"] == "internal: Cart is empty."


def test_no_tool_call_still_reported_when_nothing_was_ever_attempted(stub_mcp):
    result = E.run_fixture_discovery("openai", FakeClient(None), fixture(), "m", None, 6)

    assert result["fail_reason"] == "no_tool_call"
    assert result["attempted_tools"] == []
