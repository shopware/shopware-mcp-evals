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

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypedDict, cast

import pytest

from eval import runner as E
from eval.result_schema import Fixture, FixtureResult, JsonObject, McpResponse, TokenCounts, ToolDef, as_object
from tests.stubs import const

TOOL = "shopware-entity-read"

# What the `stub_exec` fixture hands over: scripted replies keyed by tool name,
# and every (tool, args) pair the runner sent.
type ExecStub = tuple[dict[str, str], list[tuple[str, JsonObject]]]


def fixture(fid: str = "f1", tool: str = TOOL, **over: object) -> Fixture:
    base: JsonObject = {
        "id": fid,
        "prompt": "Read the product.",
        "expected_tool": tool,
        "category": "unambiguous",
        **over,
    }
    return cast(Fixture, cast(object, base))


class SeenTurn(TypedDict):
    """One turn as the fake client saw it — what a test asserts the loop sent."""

    model: str
    system: str | None
    messages: list[JsonObject]
    tools: list[JsonObject]


def offered(tools: list[JsonObject]) -> set[str]:
    """Tool names out of an OpenAI-shaped catalogue, which nests them."""
    return {str(as_object(t.get("function")).get("name", "")) for t in tools}


class FakeClient:
    """Records what it was asked and replies with a scripted tool call."""

    def __init__(
        self, tool_name: str | None = TOOL, *, raises: Exception | None = None, tokens: tuple[int, int] = (120, 8)
    ) -> None:
        self.tool_name: str | None = tool_name
        self.raises: Exception | None = raises
        self.tokens: tuple[int, int] = tokens
        self.seen: list[SeenTurn] = []


def fake_turn(
    client: FakeClient, model: str, system_prompt: str | None, messages: list[JsonObject], tools: list[JsonObject]
) -> E.Turn:
    if client.raises:
        raise client.raises
    client.seen.append(SeenTurn(model=model, system=system_prompt, messages=messages, tools=tools))
    calls = [E.ToolCall(id="c1", name=client.tool_name, input={"entity": "product"})] if client.tool_name else []
    return {
        "tool_calls": calls,
        "assistant_message": {"role": "assistant", "content": None},
        "tool_result_builder": lambda r: [],
        "stop_reason": "tool_calls" if calls else "stop",
        "tokens": TokenCounts(input=client.tokens[0], output=client.tokens[1]),
    }


def dispatch_turn(
    client: FakeClient | ScriptedClient,
    model: str,
    system_prompt: str | None,
    messages: list[JsonObject],
    tools: list[JsonObject],
) -> E.Turn:
    """Route to whichever fake the client is: FakeClient repeats one call,
    ScriptedClient plays a sequence. One dispatcher so no test has to remember
    to swap the turn function in as well as the client."""
    if isinstance(client, ScriptedClient):
        return scripted_turn(client, model, system_prompt, messages, tools)
    return fake_turn(client, model, system_prompt, messages, tools)


# The stubbed MCP layer hands its reply back through the response envelope rather
# than a closure, so mcp_result_text can be one shared stub for every fixture
# below instead of one per test.
REPLY_KEY = "_reply"


def reply(text: str, error: str = "") -> McpResponse:
    envelope: JsonObject = {REPLY_KEY: text}
    if error:
        envelope["_err"] = error
    return cast(McpResponse, cast(object, envelope))


def replied_text(resp: McpResponse) -> str:
    return str(cast(JsonObject, cast(object, resp)).get(REPLY_KEY, ""))


def replied_error(resp: McpResponse) -> str:
    return str(cast(JsonObject, cast(object, resp)).get("_err", ""))


def no_error(_resp: McpResponse) -> str:
    return ""


@pytest.fixture(autouse=True)
def stub_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "anthropic_turn", dispatch_turn)
    monkeypatch.setattr(E, "openai_turn", dispatch_turn)


@pytest.fixture
def stub_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery opens a session and reads the advertised surface before its first
    turn, so both have to answer for the fixture to reach the fake client.

    `mcp_call` is stubbed too because the answering call is now executed, not
    just graded — an unstubbed one would reach the network.
    """

    def init(endpoint: object = None) -> tuple[str, str]:
        assert endpoint is None or endpoint is E.ADMIN
        return "sid", ""

    def list_all(_session: str, endpoint: object = None) -> list[ToolDef]:
        assert endpoint is None or endpoint is E.ADMIN
        return [ToolDef(name=TOOL, description="d", inputSchema={})]

    def call(*_args: object, **_kwargs: object) -> McpResponse:
        return reply('{"data": [{"id": "x"}]}')

    monkeypatch.setattr(E, "mcp_init", init)
    monkeypatch.setattr(E, "mcp_tools_list_all", list_all)
    monkeypatch.setattr(E, "mcp_call", call)
    monkeypatch.setattr(E, "mcp_call_error", no_error)
    monkeypatch.setattr(E, "mcp_result_text", replied_text)


# ---------------------------------------------------------------------------
# Provider tool-schema adapters
# ---------------------------------------------------------------------------
def test_anthropic_adapter_uses_input_schema() -> None:
    out = E.tools_for_anthropic([{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])

    assert out == [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]


def test_openai_adapter_nests_under_function() -> None:
    out = E.tools_for_openai([{"name": "t", "description": "d", "inputSchema": {"type": "object"}}])

    assert out == [
        {"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}
    ]


@pytest.mark.parametrize("adapter,key", [(E.tools_for_anthropic, "input_schema"), (E.tools_for_openai, None)])
def test_a_tool_without_a_schema_gets_an_empty_object_not_null(
    adapter: Callable[[list[ToolDef]], list[JsonObject]], key: str | None
) -> None:
    """OpenAI rejects a null or list-typed `properties`, so the default has to be
    a real empty object."""
    out = adapter([ToolDef(name="t")])
    schema = out[0][key] if key else as_object(out[0].get("function")).get("parameters")

    assert schema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# System-prompt routing, which differs per provider
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("stub_mcp")
def test_openai_carries_the_system_prompt_in_the_messages() -> None:
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(), "m", "SYSTEM", 6)

    assert client.seen[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert client.seen[0]["system"] is None


@pytest.mark.usefixtures("stub_mcp")
def test_anthropic_carries_the_system_prompt_as_a_parameter() -> None:
    client = FakeClient()

    E.run_fixture_discovery("anthropic", client, fixture(), "m", "SYSTEM", 6)

    assert client.seen[0]["system"] == "SYSTEM"
    # The agentic loop appends to the same list it handed over, so assert the
    # invariant rather than the exact contents: Anthropic must get no system
    # message, because it already has the prompt as a parameter.
    assert client.seen[0]["messages"][0]["role"] == "user"
    assert not [m for m in client.seen[0]["messages"] if m["role"] == "system"]


@pytest.mark.usefixtures("stub_mcp")
def test_a_fixture_reaching_its_expected_tool_passes() -> None:
    """The happy path through run_fixture_discovery: the first non-meta call is
    terminal and graded, and it is not executed."""
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL and result["passed"] is True
    assert result["mode"] == "discovery"
    assert result.get("tokens") == {"input": 120, "cached_input": 0, "output": 8}
    assert (result.get("latency_s") or 0) >= 0


@pytest.mark.usefixtures("stub_mcp")
def test_a_wrong_tool_is_recorded_with_what_was_picked() -> None:
    result = E.run_fixture_discovery("openai", FakeClient("other-tool"), fixture(), "m", None, 6)

    assert result["selected_tool"] == "other-tool" and result["passed"] is False
    assert result.get("fail_reason") == "wrong_tool"


@pytest.mark.usefixtures("stub_mcp")
def test_an_alternative_from_acceptable_tools_is_accepted() -> None:
    result = E.run_fixture_discovery("openai", FakeClient("alt"), fixture(acceptable_tools=["alt"]), "m", None, 6)

    assert result["passed"] is True


@pytest.mark.usefixtures("stub_mcp")
def test_no_tool_call_at_all_is_its_own_fail_reason() -> None:
    result = E.run_fixture_discovery("openai", FakeClient(None), fixture(), "m", None, 6)

    assert result["selected_tool"] is None and result["passed"] is False
    assert result.get("fail_reason") == "no_tool_call"


@pytest.mark.usefixtures("stub_mcp")
def test_the_fixture_note_is_carried_into_the_record() -> None:
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(notes="why this matters"), "m", None, 6)

    assert result.get("notes") == "why this matters"


# ---------------------------------------------------------------------------
# Result records for fixtures that never ran
# ---------------------------------------------------------------------------
def test_skipped_result_is_excluded_from_scoring_not_counted_as_a_failure() -> None:
    r = E.skipped_result(fixture(), "discovery")

    assert r.get("skipped") is True and r["passed"] is False
    assert "not registered on this instance" in r.get("skip_reason", "")
    assert E.scored([r]) == []


def test_error_result_is_graded_but_not_gating() -> None:
    r = E.error_result(fixture(), "discovery", RuntimeError("500 Server Error"))

    assert r.get("error") == "500 Server Error" and r["passed"] is False
    assert len(E.scored([r])) == 1, "it counts against the error budget"
    assert E.executed([r]) == [], "but it must not count against the pass rate"


def test_a_discovery_error_record_carries_the_discovery_fields() -> None:
    """discovery_summary indexes steps and discovery_path directly, so an errored
    discovery fixture without them would crash the summary."""
    r = E.error_result(fixture(), "discovery", RuntimeError("boom"))

    assert r.get("steps") == 0 and r.get("discovery_path") == "none"
    assert r.get("search_hit") is None and r.get("enabled_correct_toolset") is None


def test_an_error_record_for_another_mode_omits_the_discovery_fields() -> None:
    """Discovery is the only mode the runner has, but the helper stays generic, so
    a caller passing anything else must not get half-populated discovery keys."""
    assert "discovery_path" not in E.error_result(fixture(), "other", RuntimeError("boom"))


# ---------------------------------------------------------------------------
# tool-search payload parsing
# ---------------------------------------------------------------------------
# The inline tool definitions are what make a search-surfaced tool callable, so
# these assert on `_search_rows`' `tool` — there used to be a
# `_search_result_tools` wrapper that only these tests called.
def _tool_names(text: str) -> list[str]:
    rows, _ = E._search_rows(text)
    return [row["tool"]["name"] for row in rows]


def test_search_results_extract_the_inline_tool_definitions() -> None:
    text = '{"data": [{"tool": {"name": "a", "description": "d"}}, {"tool": {"name": "b"}}]}'

    assert _tool_names(text) == ["a", "b"]


def test_search_results_of_a_non_json_body_are_empty() -> None:
    assert _tool_names("not json") == []


def test_search_results_tolerate_entries_without_a_tool() -> None:
    text = '{"data": [{"score": 1}, {"tool": null}, {"tool": {"name": "ok"}}, "bare"]}'

    assert _tool_names(text) == ["ok"]


def test_search_results_drop_a_tool_with_no_name() -> None:
    """A nameless tool cannot be called or matched, so it is not a result."""
    assert _tool_names('{"data": [{"tool": {"description": "d"}}]}') == []


def test_search_results_of_an_empty_payload_are_empty() -> None:
    assert _tool_names("{}") == []


def test_search_rows_carry_rank_score_and_pool_size() -> None:
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


def test_search_rows_rank_by_server_order_not_by_score() -> None:
    """Ranking is the server's job. Re-sorting here would measure our idea of
    relevance instead of the one the model was actually shown."""
    text = json.dumps({"data": [{"tool": {"name": "low"}, "score": 1}, {"tool": {"name": "high"}, "score": 99}]})

    rows, _ = E._search_rows(text)

    assert [r["tool"]["name"] for r in rows] == ["low", "high"]
    assert [r["rank"] for r in rows] == [1, 2]


def test_search_rows_skip_unusable_entries_without_shifting_later_ranks() -> None:
    """Rank is the position the server returned, so a dropped nameless entry
    must not renumber the rows after it — that would report a tool as ranking
    better than it did."""
    text = json.dumps({"data": [{"tool": {"description": "nameless"}}, {"tool": {"name": "real"}}]})

    rows, _ = E._search_rows(text)

    assert [(r["tool"]["name"], r["rank"]) for r in rows] == [("real", 2)]


def test_search_rows_of_an_unparseable_or_empty_body() -> None:
    assert E._search_rows("not json") == ([], None)
    assert E._search_rows("{}") == ([], None)
    assert E._search_rows('{"data": [], "_meta": "not a dict"}') == ([], None)


# ---------------------------------------------------------------------------
# Search rank recorded across a whole fixture run
# ---------------------------------------------------------------------------
class ScriptedClient:
    """Plays one scripted tool call per turn."""

    def __init__(self, script: list[tuple[str, JsonObject]]) -> None:
        self.script: list[tuple[str, JsonObject]] = list(script)
        self.raises: Exception | None = None
        self.tokens: tuple[int, int] = (10, 1)
        self.seen: list[SeenTurn] = []


def scripted_turn(
    client: ScriptedClient,
    _model: str,
    _system_prompt: str | None,
    _messages: list[JsonObject],
    _tools: list[JsonObject],
) -> E.Turn:
    """Plays the next scripted call, then answers in prose once out of script —
    which is how a model that has decided to decline actually behaves."""
    if not client.script:
        return {
            "tool_calls": [],
            "assistant_message": {"role": "assistant", "content": "Nothing here can do that."},
            "tool_result_builder": lambda r: [],
            "stop_reason": "stop",
            "tokens": TokenCounts(input=5, output=5),
        }
    name, payload = client.script.pop(0)
    return {
        "tool_calls": [E.ToolCall(id=f"c{len(client.seen)}", name=name, input=payload)],
        "assistant_message": {"role": "assistant", "content": None},
        "tool_result_builder": lambda r: [],
        "stop_reason": "tool_calls",
        "tokens": TokenCounts(input=10, output=1),
    }


def search_payload(names: list[str], *, candidates: int = 27, scores: dict[str, float] | None = None) -> str:
    scores = scores or {}
    return json.dumps(
        {
            "data": [{"tool": {"name": n, "description": "d"}, "score": scores.get(n, 1.0)} for n in names],
            "_meta": {"totalCandidates": candidates},
        }
    )


@pytest.fixture
def stub_search(monkeypatch: pytest.MonkeyPatch, stub_mcp: None) -> list[str]:
    """Feed scripted tool-search payloads back as if the server answered."""
    del stub_mcp  # depended on for its patching, not its value
    replies: list[str] = []

    def call(*_args: object, **_kwargs: object) -> McpResponse:
        return reply(replies.pop(0) if replies else "{}")

    monkeypatch.setattr(E, "mcp_call", call)
    monkeypatch.setattr(E, "mcp_call_error", no_error)
    monkeypatch.setattr(E, "mcp_result_text", replied_text)
    return replies


def test_best_rank_across_several_searches_is_the_one_recorded(stub_search: list[str]) -> None:
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

    assert result.get("search_rank") == 2
    assert result.get("search_score") == 8.5
    assert result.get("search_candidates") == 27
    assert result.get("search_hit") is True
    assert result["passed"] is True


def test_a_search_that_never_surfaces_the_tool_has_no_rank(stub_search: list[str]) -> None:
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

    assert result.get("search_hit") is False
    assert result.get("search_rank") is None
    assert result.get("search_score") is None


@pytest.mark.usefixtures("stub_mcp")
def test_a_fixture_that_never_searches_records_no_rank() -> None:
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result.get("search_rank") is None
    assert result.get("search_candidates") is None


# ---------------------------------------------------------------------------
# Negative fixtures end to end
# ---------------------------------------------------------------------------
def negative_fixture(**over: object) -> Fixture:
    base: JsonObject = {
        "id": "neg1",
        "category": "negative",
        "prompt": "Publish this to our Instagram account.",
        "expect_no_tool": True,
        **over,
    }
    return cast(Fixture, cast(object, base))


@pytest.mark.usefixtures("stub_mcp")
def test_a_negative_fixture_passes_when_the_model_answers_in_prose() -> None:
    """No tool call is the pass, and it must not carry a failure reason."""
    result = E.run_fixture_discovery("openai", FakeClient(None), negative_fixture(), "m", None, 6)

    assert result["passed"] is True
    assert result["selected_tool"] is None
    assert result.get("fail_reason") is None
    assert result["expected_tool"] is None


@pytest.mark.usefixtures("stub_mcp")
def test_a_negative_fixture_fails_when_any_tool_is_reached_for() -> None:
    result = E.run_fixture_discovery("openai", FakeClient(TOOL), negative_fixture(), "m", None, 6)

    assert result["passed"] is False
    assert result["selected_tool"] == TOOL


def test_a_negative_fixture_fails_on_the_step_cap(stub_search: list[str]) -> None:
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

    assert result.get("fail_reason") == "step_cap"
    assert result["passed"] is False


def test_a_negative_fixture_may_search_first_and_still_pass(stub_search: list[str]) -> None:
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
    assert result.get("discovery_path") == "none"
    assert [m["tool"] for m in result.get("meta_calls") or []] == ["shopware-tool-search"]


def test_payload_bytes_and_surface_tokens_are_recorded(stub_search: list[str]) -> None:
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
    assert (result.get("payload_bytes") or 0) > search_bytes
    surface, peak = result.get("surface_tokens") or 0, result.get("surface_tokens_peak") or 0
    assert surface > 0
    assert peak > surface, "search added a tool to the surface"


# ---------------------------------------------------------------------------
# Execution, assertions and recovery
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_exec(monkeypatch: pytest.MonkeyPatch, stub_mcp: None) -> ExecStub:
    """Scripted server replies for the answering call, keyed by tool name."""
    del stub_mcp  # depended on for its patching, not its value
    replies: dict[str, str] = {}

    calls: list[tuple[str, JsonObject]] = []

    def call(_session: str, name: str, args: JsonObject, endpoint: object = None) -> McpResponse:
        assert endpoint is None or endpoint is E.ADMIN
        calls.append((name, args))
        return reply(replies.get(name, '{"data": [{"id": "x"}]}'))

    monkeypatch.setattr(E, "mcp_call", call)
    monkeypatch.setattr(E, "mcp_call_error", replied_error)
    monkeypatch.setattr(E, "mcp_result_text", replied_text)
    return replies, calls


def test_the_answering_call_is_executed_not_just_graded(
    stub_exec: ExecStub,
) -> None:
    """The gap this closes: a correctly named tool with nonsense arguments used
    to score the same as one that runs."""
    _, calls = stub_exec

    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert (TOOL, {"entity": "product"}) in calls
    assert result.get("execution") == "executed"
    assert result["passed"] is True
    assert result.get("first_try") is True


@pytest.mark.usefixtures("stub_exec")
def test_a_correctly_named_call_the_server_rejects_now_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "mcp_call", const(reply("", "Validation failed: entity is required")))

    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL, "it named the right tool"
    assert result["passed"] is False, "but the call did not work"
    assert result.get("fail_reason") == "invalid_arguments"


@pytest.mark.usefixtures("stub_exec")
def test_a_wrong_first_pick_can_be_recovered_from() -> None:
    """The signal the suite could not previously produce: picking wrong and then
    correcting is materially different from never getting there."""
    client = ScriptedClient([("shopware-entity-schema", {}), (TOOL, {"entity": "product"})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert result["passed"] is True
    assert result.get("first_try") is False
    assert result.get("recovered") is True
    assert result.get("first_tool_correct") is False
    assert result.get("wrong_calls") == 1
    assert [a["tool"] for a in result.get("attempted_tools") or []] == ["shopware-entity-schema", TOOL]
    assert result.get("steps_to_correct") == 2


@pytest.mark.usefixtures("stub_exec")
def test_selected_tool_stays_the_first_answer_after_a_recovery() -> None:
    """`selected_tool` keeps its old meaning so historical reports and the
    per-tool scorecard still compare like with like."""
    client = ScriptedClient([("shopware-entity-schema", {}), (TOOL, {})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert result["selected_tool"] == "shopware-entity-schema"
    assert result["passed"] is True


@pytest.mark.usefixtures("stub_exec")
def test_flailing_until_the_step_cap_is_not_a_recovery() -> None:
    client = ScriptedClient([("shopware-entity-schema", {}), ("shopware-entity-aggregate", {})])

    result = E.run_fixture_discovery("openai", client, fixture(), "m", None, 2)

    assert result["passed"] is False
    assert result.get("recovered") is False
    assert result.get("wrong_calls") == 2


def test_a_mutating_tool_is_executed_with_dry_run_forced_on(
    stub_exec: ExecStub,
) -> None:
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
    assert result.get("dry_run_forced") is True
    assert result["passed"] is True


def test_an_unsafe_tool_is_graded_on_selection_and_never_called(
    stub_exec: ExecStub,
) -> None:
    """media-upload, cart-manage and scaffold mutate with no dryRun, so they
    keep the old selection-only grading rather than being executed."""
    _, calls = stub_exec

    result = E.run_fixture_discovery(
        "openai", FakeClient("shopware-media-upload"), fixture(tool="shopware-media-upload"), "m", None, 6
    )

    assert calls == [], "nothing was sent to the server"
    assert result.get("execution") == "skipped_unsafe"
    assert result["passed"] is True, "graded on selection, as before"
    assert (result.get("attempted_tools") or [{}])[0].get("executed") is False


def test_an_unknown_tool_is_not_executed_either(stub_exec: ExecStub) -> None:
    _, calls = stub_exec

    result = E.run_fixture_discovery("openai", FakeClient("tool-shipped-yesterday"), fixture(), "m", None, 6)

    assert calls == []
    assert result.get("execution") == "skipped_unclassified"
    assert result["passed"] is False


@pytest.mark.usefixtures("stub_exec")
def test_a_negative_fixture_that_calls_a_tool_does_not_get_to_recover() -> None:
    """Calling anything IS the failure, so there is nothing to recover from."""
    result = E.run_fixture_discovery("openai", FakeClient(TOOL), negative_fixture(), "m", None, 6)

    assert result["passed"] is False
    assert result["selected_tool"] == TOOL


def test_a_data_tier_fixture_fails_on_an_empty_result(
    stub_exec: ExecStub,
) -> None:
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
    assert result.get("fail_reason") == "too_few:data<1"


# ---------------------------------------------------------------------------
# Diagnostic arms
# ---------------------------------------------------------------------------
class Enabled(TypedDict):
    """What an arm enabled before the model saw anything: the individual toolsets
    by name, and how many times enable-all was called."""

    one: list[str]
    all: int


@pytest.fixture
def stub_arms(monkeypatch: pytest.MonkeyPatch, stub_exec: ExecStub) -> Enabled:
    """Record which toolsets an arm enabled before the model saw anything."""
    del stub_exec  # depended on for its patching, not its value
    enabled = Enabled(one=[], all=0)

    def enable_one(_session: str, toolset: str, endpoint: object = None) -> None:
        assert endpoint is None or endpoint is E.ADMIN
        enabled["one"].append(toolset)

    def enable_all(_session: str, endpoint: object = None) -> None:
        assert endpoint is None or endpoint is E.ADMIN
        enabled["all"] += 1

    def list_all(_session: str, endpoint: object = None) -> list[ToolDef]:
        assert endpoint is None or endpoint is E.ADMIN
        return [
            ToolDef(name=TOOL, description="d", inputSchema={}),
            ToolDef(name="shopware-tool-search", description="d", inputSchema={}),
        ]

    monkeypatch.setattr(E, "enable_toolset", enable_one)
    monkeypatch.setattr(E, "enable_all_toolsets", enable_all)
    monkeypatch.setattr(E, "mcp_tools_list_all", list_all)
    return enabled


def test_the_isolated_arm_pre_enables_only_the_fixtures_own_group(stub_arms: Enabled) -> None:
    E.run_fixture_discovery("openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="isolated")

    assert stub_arms["one"] == ["entity"]
    assert stub_arms["all"] == 0


def test_the_full_arm_enables_everything(stub_arms: Enabled) -> None:
    E.run_fixture_discovery("openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="full")

    assert stub_arms["all"] == 1
    assert stub_arms["one"] == []


@pytest.mark.usefixtures("stub_arms")
@pytest.mark.parametrize("arm", ["isolated", "full"])
def test_the_diagnostic_arms_withhold_the_meta_tools(arm: str) -> None:
    """The fix for the bug that killed `baseline`: with no meta-tool advertised
    there is no meta-call to misgrade as a wrong answer."""
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(expected_toolset="entity"), "m", None, 6, arm=arm)

    names = offered(client.seen[0]["tools"])
    assert "shopware-tool-search" not in names
    assert TOOL in names


@pytest.mark.usefixtures("stub_arms")
def test_the_discovery_arm_still_offers_them() -> None:
    client = FakeClient()

    E.run_fixture_discovery("openai", client, fixture(), "m", None, 6)

    assert "shopware-tool-search" in offered(client.seen[0]["tools"])


@pytest.mark.usefixtures("stub_arms")
def test_the_arm_is_recorded_on_the_record() -> None:
    result = E.run_fixture_discovery(
        "openai", FakeClient(), fixture(expected_toolset="entity"), "m", None, 6, arm="full"
    )
    assert result["mode"] == "full"


# ---------------------------------------------------------------------------
# Triage: only the failures, and only the categories the arms can speak to
# ---------------------------------------------------------------------------
def triage(discovery: list[FixtureResult], fixtures: list[Fixture]) -> dict[str, list[FixtureResult]]:
    return E.triage_arms("openai", FakeClient(), discovery, fixtures, "m", None, 6)


def disc(fid: str, passed: bool, **extra: object) -> FixtureResult:
    base: JsonObject = {"id": fid, "passed": passed, **extra}
    return cast(FixtureResult, cast(object, base))


@pytest.mark.usefixtures("stub_arms")
def test_only_discovery_failures_are_re_run(capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = [fixture("won", expected_toolset="entity"), fixture("lost", expected_toolset="entity")]

    out = triage([disc("won", True), disc("lost", False)], fixtures)
    capsys.readouterr()

    assert [r["id"] for r in out["isolated"]] == ["lost"]
    assert [r["id"] for r in out["full"]] == ["lost"]


@pytest.mark.usefixtures("stub_arms")
def test_a_clean_run_triages_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    out = triage([disc("won", True)], [fixture("won", expected_toolset="entity")])
    assert out == {}
    assert "no discovery failures" in capsys.readouterr().out


@pytest.mark.usefixtures("stub_arms")
def test_errored_fixtures_are_not_triaged(capsys: pytest.CaptureFixture[str]) -> None:
    """A 500 is missing data, not a description problem — re-running it under
    two more arms just spends money on the same 500."""
    out = triage([disc("boom", False, error="500")], [fixture("boom", expected_toolset="entity")])
    capsys.readouterr()
    assert out == {}


@pytest.mark.usefixtures("stub_arms")
@pytest.mark.parametrize("category", ["meta", "discovery", "negative"])
def test_categories_the_arms_cannot_speak_to_are_skipped(capsys: pytest.CaptureFixture[str], category: str) -> None:
    """meta wants a withheld tool, discovery exists to exercise the layer the
    arms bypass, and pre-enabling a group to ask 'does anything bite' is a
    different question."""
    fixtures = [fixture("f", category=category, expected_toolset="entity")]

    out = triage([disc("f", False)], fixtures)
    capsys.readouterr()

    assert out == {}


@pytest.mark.usefixtures("stub_arms")
def test_an_arm_that_cannot_advertise_the_tool_reports_setup_failure() -> None:
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

    assert result.get("skipped") is True
    assert "arm setup failed" in result.get("skip_reason", "")
    assert "nonexistent" in result.get("skip_reason", "")


@pytest.mark.usefixtures("stub_mcp")
def test_the_discovery_arm_never_reports_setup_failure() -> None:
    """Discovery starts from the default surface by design — the tool being
    absent is the thing it measures, not a broken experiment."""
    result = E.run_fixture_discovery("openai", FakeClient(), fixture(), "m", None, 6)

    assert not result.get("skipped")


@pytest.mark.usefixtures("stub_exec")
def test_a_failed_call_keeps_its_reason_when_the_model_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug that hid a broken assertion tier for three runs.

    The model called the right tool, the call failed, and it answered in prose
    on the next turn — so `no_tool_call` overwrote the real reason and every
    report said the model had chosen nothing.
    """
    monkeypatch.setattr(E, "mcp_call", const(reply("", "Missing required parameter: id")))

    result = E.run_fixture_discovery("openai", ScriptedClient([(TOOL, {})]), fixture(), "m", None, 6)

    assert result["selected_tool"] == TOOL, "it did choose a tool"
    assert result.get("fail_reason") == "invalid_arguments", "and the informative reason survives"
    assert str((result.get("attempted_tools") or [{}])[0].get("error", "")).startswith("Missing required parameter")


def test_an_in_band_failure_records_what_the_server_said(
    stub_exec: ExecStub,
) -> None:
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

    assert (result.get("attempted_tools") or [{}])[0].get("reason") == "tool_error"
    assert (result.get("attempted_tools") or [{}])[0].get("error") == "internal: Cart is empty."


@pytest.mark.usefixtures("stub_mcp")
def test_no_tool_call_still_reported_when_nothing_was_ever_attempted() -> None:
    result = E.run_fixture_discovery("openai", FakeClient(None), fixture(), "m", None, 6)

    assert result.get("fail_reason") == "no_tool_call"
    assert result.get("attempted_tools") == []
