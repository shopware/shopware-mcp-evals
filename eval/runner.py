#!/usr/bin/env python3
"""
Shopware MCP LLM Eval Runner (MCP Server v2: dynamic tool discovery)

Runs each fixture in discovery mode: only the default advertised surface is
passed to the model, and the runner executes discovery meta-tool calls
(shopware-tool-search, shopware-toolsets-list, shopware-toolset-enable) for real
against the server, feeding results back in an agentic loop. The first NON-meta
tool call is terminal and graded against expected_tool. Meta steps are free but
counted.

This is the only mode, because it is the only one the server can be in: every
tool outside the `discovery` group is deferred, so a fresh session never sees
the full catalogue.

There used to be a `baseline` mode that enabled all toolsets and graded the
first call of a single request, as a v1 comparison reference. It was removed: it
kept the discovery meta-tools in the catalogue it handed the model but graded the
first call without exempting them, so a model that followed the server's own
instructions and called shopware-toolsets-list was scored as picking the wrong
tool. 40 of its 42 failures on the primary model were that artifact, which made
its per-tool "effect" column read as evidence for progressive disclosure when it
was measuring the grading difference between the two modes.

Usage:
    python -m eval.runner                                  # both modes, Anthropic
    python -m eval.runner --provider openai --model gpt-5.4-mini
    python -m eval.runner --modes discovery --max-steps 8
    python -m eval.runner --category disambiguation
    python -m eval.runner --id disambig_count_vs_search
"""

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast

import requests
import yaml

import lane
from eval.assertions import check, inband_error
from eval.cost import load_pricing, run_cost
from eval.report import (
    BOLD,
    DIM,
    RED,
    RESET,
    print_discovery_block,
    print_single_mode,
    print_tier_block,
    render_line,
)
from eval.result_schema import (
    SCHEMA_VERSION,
    AttemptRecord,
    Fixture,
    FixtureResult,
    GateVerdict,
    JsonObject,
    MetaCall,
    ModeBlock,
    PromptInventory,
    Report,
    SkippedFixture,
    SummaryRow,
    TokenCounts,
    ToolDef,
    ToolHealth,
    as_list,
    as_object,
)
from eval.scoring import (
    count_rate_limited,
    discovery_summary,
    executed,
    gate_verdict,
    is_correct,
    is_negative,
    scored,
)
from mcp_client import (
    ADMIN,
    BASE,
    META_TOOLS,
    SW_ACCESS_KEY,
    SW_BASE_URL,
    SW_SC_ACCESS_KEY,
    SW_SECRET_ACCESS_KEY,
    Endpoint,
    enable_all_toolsets,
    enable_toolset,
    endpoint_by_name,
    mcp_call,
    mcp_call_error,
    mcp_fetch_context_prompts,
    mcp_init,
    mcp_result_text,
    mcp_tools_list_all,
)
from ownership import CORE, PROMPT_SETS, breakdown, owner_of
from toolclass import classify, is_executable, prepare_call

# A placeholder resolver reads one id off the live lane; a seeding resolver
# WRITES and yields the several ids it filled from one cart.
type LaneResolver = Callable[[Endpoint], str | None]
type SeedingResolver = Callable[[Endpoint], dict[str, str]]

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# GitHub Models: an OpenAI-compatible endpoint authenticated with the workflow's
# built-in GITHUB_TOKEN (needs `models: read`). It costs nothing and carries
# non-OpenAI publishers, so it gives a cross-provider second opinion without
# provisioning an API key. Its catalogue has no Anthropic models.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"

# LM Studio: an OpenAI-compatible server on the developer's own machine. Same
# adapter and the same turn function as `openai` and `github` — only the base URL
# and the credential differ, which is the pattern `github` already proved.
#
# It exists so the whole suite can be exercised against the trunk lane for free
# before anything reaches CI. The models are weaker than the CI ones and the
# numbers are not comparable to them; what it validates is the harness — fixtures
# load, tools resolve, assertions fire, the report renders — which is most of
# what breaks.
#
# The key is required by the SDK and ignored by the server, so it defaults to a
# placeholder rather than making everyone invent one.
LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
LMSTUDIO_API_KEY = os.environ.get("LMSTUDIO_API_KEY", "lm-studio")

# ---------------------------------------------------------------------------
# Provider adapters
#
# The clients are described structurally rather than by SDK class. Two reasons,
# and both are load-bearing: the SDKs are imported lazily so a run with one
# provider does not need the other's package, and the adapter tests drive these
# functions with hand-built fakes. A Protocol is the honest statement of what
# either has to provide, and it holds the fakes to the same contract.
# ---------------------------------------------------------------------------


class _AnthropicUsage(Protocol):
    input_tokens: int
    output_tokens: int


class _AnthropicBlock(Protocol):
    type: str
    id: str
    name: str
    input: JsonObject


class _AnthropicReply(Protocol):
    content: list[_AnthropicBlock]
    stop_reason: str | None
    usage: _AnthropicUsage


class _AnthropicMessages(Protocol):
    def create(self, **kwargs: object) -> _AnthropicReply: ...


class _AnthropicClient(Protocol):
    @property
    def messages(self) -> _AnthropicMessages: ...


class _OpenAIFunction(Protocol):
    name: str
    arguments: str


class _OpenAIToolCall(Protocol):
    id: str
    function: _OpenAIFunction


class _OpenAIMessage(Protocol):
    content: str | None
    tool_calls: list[_OpenAIToolCall] | None


class _OpenAIChoice(Protocol):
    message: _OpenAIMessage
    finish_reason: str | None


class _OpenAIUsage(Protocol):
    prompt_tokens: int
    completion_tokens: int


class _OpenAIReply(Protocol):
    choices: list[_OpenAIChoice]
    usage: _OpenAIUsage


class _OpenAICompletions(Protocol):
    def create(self, **kwargs: object) -> _OpenAIReply: ...


class _OpenAIChat(Protocol):
    @property
    def completions(self) -> _OpenAICompletions: ...


class _OpenAIClient(Protocol):
    @property
    def chat(self) -> _OpenAIChat: ...


class ToolCall(TypedDict):
    """One tool call the model asked for, normalised across providers."""

    id: str
    name: str
    input: JsonObject


class Turn(TypedDict):
    """What one assistant turn yields, in provider-independent form.

    `tool_result_builder` differs in shape between the two: Anthropic wants the
    results as content blocks of a single user message, OpenAI as one message
    per result. The loop handles either by checking for a list.
    """

    tool_calls: list[ToolCall]
    assistant_message: JsonObject
    tool_result_builder: Callable[[list[tuple[str, str]]], JsonObject | list[JsonObject]]
    stop_reason: str | None
    tokens: TokenCounts


def _sdk_attr(obj: object, name: str) -> object:
    """One attribute an SDK response may or may not carry.

    Read through `object` so the SDKs' `Any` stops here rather than spreading
    into the token accounting.
    """
    return cast(object, getattr(obj, name, None))


def _sdk_int(obj: object, name: str) -> int:
    """An integer field that is simply absent on older replies and on the test
    fakes — the cached-token counters, both of which default to zero."""
    value = _sdk_attr(obj, name)
    return value if isinstance(value, int) else 0


def tools_for_anthropic(mcp_tools: list[ToolDef]) -> list[JsonObject]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]


def tools_for_openai(mcp_tools: list[ToolDef]) -> list[JsonObject]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in mcp_tools
    ]


def anthropic_turn(
    client: object, model: str, system_prompt: str | None, messages: list[JsonObject], tools: list[JsonObject]
) -> Turn:
    """One assistant turn. Returns {tool_calls, assistant_message, tool_result_builder,
    stop_reason, tokens}."""
    sdk = cast(_AnthropicClient, client)
    kwargs: JsonObject = {"model": model, "max_tokens": 1024, "tools": tools, "messages": messages}
    if system_prompt:
        kwargs["system"] = system_prompt
    response = sdk.messages.create(**kwargs)

    tool_calls = [
        ToolCall(id=block.id, name=block.name, input=block.input)
        for block in response.content
        if block.type == "tool_use"
    ]
    return {
        "tool_calls": tool_calls,
        "assistant_message": {"role": "assistant", "content": response.content},
        "tool_result_builder": lambda results: {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text} for call_id, text in results],
        },
        "stop_reason": response.stop_reason,
        # Anthropic's `input_tokens` is the uncached remainder — cache reads are
        # reported separately and are NOT included in it. The OpenAI adapter
        # below has to subtract instead. Getting this backwards silently
        # double-counts or under-counts the bill, which is why both adapters
        # normalise to the same three buckets here rather than at the far end.
        "tokens": TokenCounts(
            input=response.usage.input_tokens,
            cached_input=_sdk_int(response.usage, "cache_read_input_tokens"),
            output=response.usage.output_tokens,
        ),
    }


# Which output-cap parameter a model accepts, discovered once per model.
# gpt-4o and gpt-4.1 take either; every GPT-5 and o-series model rejects the old
# `max_tokens` outright. Probing rather than hardcoding keeps third-party
# OpenAI-compatible endpoints (the `github` provider) working — Mistral there
# only knows `max_tokens`.
_OUTPUT_CAP_PARAM: dict[str, str] = {}


def openai_turn(
    client: object,
    model: str,
    _system_prompt: str | None,  # carried in `messages`; kept so both turns share one signature
    messages: list[JsonObject],
    tools: list[JsonObject],
) -> Turn:
    """One assistant turn (system prompt must already be in messages)."""
    sdk = cast(_OpenAIClient, client)
    kwargs: JsonObject = {"model": model, "tools": tools, "tool_choice": "auto", "messages": messages}
    param = _OUTPUT_CAP_PARAM.get(model, "max_completion_tokens")
    try:
        response = sdk.chat.completions.create(**kwargs, **{param: 1024})
    except Exception as exc:  # noqa: BLE001 — retried below, re-raised if it isn't the cap param
        other = "max_tokens" if param == "max_completion_tokens" else "max_completion_tokens"
        if model in _OUTPUT_CAP_PARAM or param not in str(exc):
            raise
        response = sdk.chat.completions.create(**kwargs, **{other: 1024})
        param = other
    _OUTPUT_CAP_PARAM[model] = param
    msg = response.choices[0].message

    tool_calls: list[ToolCall] = []
    for call in msg.tool_calls or []:
        try:
            call_input = as_object(cast(object, json.loads(call.function.arguments)))
        except json.JSONDecodeError:
            call_input = {}
        tool_calls.append(ToolCall(id=call.id, name=call.function.name, input=call_input))

    assistant_message: JsonObject = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        assistant_message["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in msg.tool_calls
        ]
    # OpenAI's `prompt_tokens` INCLUDES the cached prefix, so the full-price
    # bucket is the difference — the opposite of Anthropic above, where
    # `input_tokens` already excludes it. OpenAI caches prompts automatically
    # over ~1k tokens with no opt-in, so this is not zero even though this
    # harness never sets cache_control: it is a discount we receive whether or
    # not we asked for it, and ignoring it would overstate the bill.
    cached = _sdk_int(_sdk_attr(response.usage, "prompt_tokens_details"), "cached_tokens")
    return {
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
        "tool_result_builder": lambda results: [
            {"role": "tool", "tool_call_id": call_id, "content": text} for call_id, text in results
        ],
        "stop_reason": response.choices[0].finish_reason,
        "tokens": TokenCounts(
            input=max(response.usage.prompt_tokens - cached, 0),
            cached_input=cached,
            output=response.usage.completion_tokens,
        ),
    }


# ---------------------------------------------------------------------------
# Discovery mode — agentic loop from the default surface
# ---------------------------------------------------------------------------


def _surface_tokens(tools: list[JsonObject]) -> int:
    """Rough token cost of the advertised tool list.

    Chars/4, deliberately: an exact count needs a tokenizer per provider and
    would change nothing about the comparison this feeds — surface at turn one
    against surface at its peak, in the same units, for the same run.
    """
    return len(json.dumps(tools)) // 4


class SearchRow(TypedDict):
    """One ranked result from shopware-tool-search.

    `rank` is the 1-indexed position the server returned it in, and it is the
    point: a boolean "was the right tool in the results" cannot tell first place
    from ninth, and that difference decides whether a model scrolling a
    20-result list ever reaches it.
    """

    tool: ToolDef
    score: float | None
    matched_in: object
    rank: int


def _search_rows(result_text: str) -> tuple[list[SearchRow], int | None]:
    """Ranked rows from shopware-tool-search, plus the candidate pool size.

    Each row is {tool, score, matchedIn, rank}, `rank` being the 1-indexed
    position the server returned it in. The rank is the point: a boolean "was
    the right tool in the results" cannot tell first place from ninth, and the
    difference decides whether a model scrolling a 20-result list ever reaches
    it. The server already computes and sends `score`/`matchedIn`; this used to
    drop both on the floor.
    """
    try:
        payload = as_object(cast(object, json.loads(result_text)))
    except (json.JSONDecodeError, TypeError):
        return [], None
    rows: list[SearchRow] = []
    for position, raw in enumerate(as_list(payload.get("data")), start=1):
        row = as_object(raw)
        tool = as_object(row.get("tool"))
        if not tool.get("name"):
            continue
        score = row.get("score")
        rows.append(
            SearchRow(
                # The row carries a whole tool definition, which is what makes a
                # search-surfaced tool callable — see DiscoveryState.record_search.
                tool=cast(ToolDef, cast(object, tool)),
                score=float(score) if isinstance(score, int | float) else None,
                matched_in=row.get("matchedIn"),
                rank=position,
            )
        )
    total = as_object(payload.get("_meta")).get("totalCandidates")
    return rows, total if isinstance(total, int) else None


@dataclass
class DiscoveryState:
    """The accumulators for one discovery run, gathered into one object instead
    of ~25 parallel locals. The loop reads and writes fields; `to_result` is the
    single place the output record (see eval/result_schema.FixtureResult) is
    assembled, so the schema and the mutation points can each be tested without
    driving a live loop.

    Loop-control values (messages, tools, catalog) stay local to the loop — they
    do not appear in the record and carrying them here would only widen the
    object without making anything clearer.
    """

    arm: str
    selected_tool: str | None = None
    selected_input: JsonObject = field(default_factory=dict)
    fail_reason: str | None = None
    # Every non-meta call the model made, in order. The first is `selected_tool`
    # (the old metric); the rest are what recovery looks like.
    attempted_tools: list[AttemptRecord] = field(default_factory=list)
    first_tool_correct: bool | None = None
    resolved: bool = False
    steps_to_correct: int | None = None
    dry_run_forced: bool = False
    execution: str | None = None
    stop: bool = False
    meta_calls: list[MetaCall] = field(default_factory=list)
    search_hit: bool | None = None
    search_rank: int | None = None
    search_score: float | None = None
    search_candidates: int | None = None
    enabled_toolsets: list[str] = field(default_factory=list)
    tokens: TokenCounts = field(default_factory=lambda: TokenCounts(input=0, cached_input=0, output=0))
    # Bytes of tool-result payload the model was made to read. A tool that
    # answers correctly but returns 40k of JSON is expensive for every client,
    # and nothing else in the suite would notice.
    payload_bytes: int = 0
    steps: int = 0
    surface_tokens: int = 0
    surface_tokens_peak: int = 0

    def add_tokens(self, turn_tokens: TokenCounts) -> None:
        self.tokens["input"] += turn_tokens.get("input", 0)
        self.tokens["output"] += turn_tokens.get("output", 0)
        self.tokens["cached_input"] = self.tokens.get("cached_input", 0) + turn_tokens.get("cached_input", 0)

    def record_search(self, result_text: str, expected_tool: str | None, catalog: dict[str, ToolDef]) -> bool:
        """Absorb a shopware-tool-search result. Tracks whether the expected tool
        was surfaced and the best rank it ever reached (a later, vaguer query
        cannot make the catalogue look worse than it is), and makes any
        search-surfaced tool callable next turn. Returns whether the catalog grew.
        """
        rows, candidates = _search_rows(result_text)
        hit = any(r["tool"].get("name") == expected_tool for r in rows)
        self.search_hit = hit if self.search_hit is None else (self.search_hit or hit)
        if candidates is not None:
            self.search_candidates = candidates
        for r in rows:
            if r["tool"].get("name") == expected_tool and (self.search_rank is None or r["rank"] < self.search_rank):
                self.search_rank, self.search_score = r["rank"], r["score"]
        changed = False
        for t in (r["tool"] for r in rows):
            if t["name"] not in catalog:
                catalog[t["name"]] = t
                changed = True
        return changed

    def record_enable(self, toolset: str, session_id: str, endpoint: Endpoint, catalog: dict[str, ToolDef]) -> bool:
        """Absorb a successful toolset-enable: record it and simulate
        tools/list_changed by re-fetching the surface. Returns whether it grew."""
        self.enabled_toolsets.append(toolset)
        changed = False
        for t in mcp_tools_list_all(session_id, endpoint=endpoint):
            if t["name"] not in catalog:
                catalog[t["name"]] = t
                changed = True
        return changed

    def to_result(self, fixture: Fixture, *, passed: bool, latency: float) -> FixtureResult:
        expected_toolset = fixture.get("expected_toolset")
        enabled_correct_toolset = None
        if expected_toolset and self.enabled_toolsets:
            enabled_correct_toolset = expected_toolset in self.enabled_toolsets

        meta_names = {m["tool"] for m in self.meta_calls}
        used_search = "shopware-tool-search" in meta_names
        used_toolsets = bool(meta_names & {"shopware-toolsets-list", "shopware-toolset-enable"})
        if self.selected_tool is None:
            discovery_path = "none"
        elif used_search and used_toolsets:
            discovery_path = "mixed"
        elif used_search:
            discovery_path = "search"
        elif used_toolsets:
            discovery_path = "toolsets"
        else:
            discovery_path = "direct"

        result: FixtureResult = {
            "schema_version": SCHEMA_VERSION,
            "id": fixture["id"],
            "category": fixture.get("category", ""),
            "mode": self.arm,
            "prompt": fixture["prompt"],
            "expected_tool": fixture.get("expected_tool"),
            "expected_toolset": expected_toolset,
            "selected_tool": self.selected_tool,
            "selected_input": self.selected_input,
            "passed": passed,
            "fail_reason": None if passed else self.fail_reason,
            # Whether the FIRST answer was the right tool. The scorecard reads
            # this for precision rather than `passed`, which since recovery no
            # longer implies the first pick was correct — a recovered fixture
            # would otherwise credit the wrong tool with a good selection.
            "first_tool_correct": self.first_tool_correct,
            # Both halves: the first answer named the right tool AND that call
            # worked. `ok` alone is only "the server accepted it", which a wrong
            # tool called competently also satisfies.
            "first_try": bool(self.attempted_tools)
            and self.attempted_tools[0]["correct"]
            and self.attempted_tools[0].get("ok", True) is True,
            "recovered": passed and len(self.attempted_tools) > 1,
            "attempted_tools": self.attempted_tools,
            "wrong_calls": sum(1 for a in self.attempted_tools if not a["correct"]),
            "steps_to_correct": self.steps_to_correct,
            "execution": self.execution,
            "dry_run_forced": self.dry_run_forced,
            "steps": self.steps,
            "meta_calls": self.meta_calls,
            "discovery_path": discovery_path,
            "search_hit": self.search_hit,
            "search_rank": self.search_rank,
            "search_score": self.search_score,
            "search_candidates": self.search_candidates,
            "enabled_toolsets": self.enabled_toolsets,
            "enabled_correct_toolset": enabled_correct_toolset,
            "latency_s": latency,
            "tokens": self.tokens,
            "payload_bytes": self.payload_bytes,
            "surface_tokens": self.surface_tokens,
            "surface_tokens_peak": self.surface_tokens_peak,
            "notes": fixture.get("notes", ""),
        }
        return result


def run_fixture_discovery(
    provider: str,
    client: object,
    fixture: Fixture,
    model: str,
    system_prompt: str | None,
    max_steps: int,
    endpoint: Endpoint = ADMIN,
    arm: str = "discovery",
) -> FixtureResult:
    prompt = fixture["prompt"]
    # Absent on a negative fixture, where no tool is the right answer. The
    # terminal set is then just `acceptable` (normally empty), which is correct:
    # every non-meta call is already terminal, so any tool the model reaches for
    # is recorded and graded as the over-trigger it is.
    expected_tool = fixture.get("expected_tool")
    acceptable = set(fixture.get("acceptable_tools", []))
    terminal_tools = ({expected_tool} | acceptable) if expected_tool else acceptable
    prov = PROVIDERS[provider]

    # Fresh session per fixture: toolset enablement persists per Mcp-Session-Id
    # and would leak across fixtures on a shared session.
    session_id, _ = mcp_init(endpoint=endpoint)

    # The arm decides what the model is shown before it says anything.
    #
    #   discovery  the default surface — three meta-tools — and the model has to
    #              find the rest. This is what a production client sees.
    #   isolated   only the group the answer lives in, so the question is purely
    #              "is this description distinguishable from its siblings".
    #   full       the whole catalogue at once: maximum collision pressure.
    #
    # The two diagnostic arms withhold the meta-tools. That is the fix for the
    # bug that killed the old `baseline` mode, which left them in the catalogue
    # and then graded a meta-call as a wrong answer — 40 of its 42 failures were
    # models correctly following the server's own instructions. With none
    # advertised, there is no meta-call to misgrade.
    if arm == "isolated" and (isolated_toolset := fixture.get("expected_toolset")):
        enable_toolset(session_id, isolated_toolset, endpoint=endpoint)
    elif arm == "full":
        enable_all_toolsets(session_id, endpoint=endpoint)

    # Callable-tool catalogue by name. Starts as the advertised default surface.
    # Grows when a toolset is enabled (re-fetched tools/list) OR when
    # shopware-tool-search returns a tool inline — a search-surfaced tool is
    # directly callable because the allowlist, not advertising, is the call
    # boundary. This mirrors how a real MCP client exposes discovered tools.
    catalog = {t["name"]: t for t in mcp_tools_list_all(session_id, endpoint=endpoint)}
    if arm != "discovery":
        catalog = {n: t for n, t in catalog.items() if n not in META_TOOLS}
        # The diagnostic arms only mean anything if the tool under test was
        # actually put in front of the model. When enabling does not surface it
        # — a toolset named differently on this endpoint, an enable that no-ops
        # — the model has nothing to pick and fails for a reason that has
        # nothing to do with the description.
        #
        # This is reported as an unusable experiment, never graded. The first
        # run of these arms against the Store endpoint produced an empty surface
        # for all 16 fixtures, and without this the matrix read every one of
        # them as "the tool's own description" — a confident answer to a
        # question that was never asked.
        if expected_tool and expected_tool not in catalog:
            record = skipped_result(fixture, arm)
            record["skip_reason"] = (
                f"arm setup failed: {expected_tool} was not advertised after enabling "
                f"{'all toolsets' if arm == 'full' else fixture.get('expected_toolset')!r} "
                f"({len(catalog)} tools offered) — this arm cannot answer anything about it"
            )
            return record
    tools = prov.tools_for(list(catalog.values()))
    # What the advertised surface costs to put in front of the model. The
    # opening figure is the price of v2's promise — a fresh session shows three
    # meta-tools, not the catalogue — and the peak is what the model actually
    # paid on later turns once discovery had pulled tools in. The gap between
    # them is the discovery layer's real context bill.
    st = DiscoveryState(arm=arm)
    st.surface_tokens = _surface_tokens(tools)
    st.surface_tokens_peak = st.surface_tokens

    messages: list[JsonObject] = []
    # Every provider except Anthropic carries the context prompt as a system
    # message; Anthropic takes it as a top-level parameter instead (see
    # anthropic_turn). This used to test `== "openai"`, which meant the `github`
    # arm ran with no context prompt at all while its report recorded
    # `system_prompt: true` — a whole provider silently measuring something else,
    # and the reason the prompt inventory below is worth recording. It is now one
    # flag on the Provider, so the two sites below cannot disagree.
    if not prov.system_as_param and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    t0 = time.time()

    while st.steps < max_steps:
        st.steps += 1
        turn = prov.turn(client, model, system_prompt if prov.system_as_param else None, messages, tools)
        st.add_tokens(turn["tokens"])

        if not turn["tool_calls"]:
            # Only when nothing was ever attempted. A model that called the
            # right tool, watched it fail and then answered in prose has a
            # far more useful reason already recorded, and overwriting it with
            # "no_tool_call" is what hid a broken assertion tier for three
            # runs: every Store failure read as "the model called nothing"
            # when it had in fact called exactly the right tool.
            if not st.attempted_tools:
                st.fail_reason = "no_tool_call"
            break

        messages.append(turn["assistant_message"])

        tool_results: list[tuple[str, str]] = []
        catalog_changed = False
        for call in turn["tool_calls"]:
            # Any non-meta call is an answer. Meta navigation tools that are NOT
            # the expected answer (e.g. shopware-toolsets-list on the way to
            # toolset-enable) fall through and are executed as discovery flow —
            # listing toolsets before enabling one is correct.
            answering = call["name"] in terminal_tools or call["name"] not in META_TOOLS
            if answering:
                if _handle_answering_call(st, call, fixture, terminal_tools, session_id, endpoint, tool_results):
                    break
                continue

            # Execute discovery meta-tools for real and feed results back.
            resp = mcp_call(session_id, call["name"], call["input"], endpoint=endpoint)
            err = mcp_call_error(resp)
            result_text = mcp_result_text(resp) or (f"Error: {err}" if err else "")
            st.payload_bytes += len(result_text.encode("utf-8"))
            tool_results.append((call["id"], result_text))
            st.meta_calls.append(MetaCall(tool=call["name"], input=call["input"], result_preview=result_text[:300]))
            if call["name"] == "shopware-tool-search":
                catalog_changed |= st.record_search(result_text, expected_tool, catalog)
            if call["name"] == "shopware-toolset-enable" and not err:
                toolset = str(call["input"].get("toolset", ""))
                catalog_changed |= st.record_enable(toolset, session_id, endpoint, catalog)

        # `stop` rather than "a tool was selected": a wrong first pick no longer
        # ends the run, because whether the model recovers from it is the point.
        if st.stop:
            break

        if catalog_changed:
            tools = prov.tools_for(list(catalog.values()))
            st.surface_tokens_peak = max(st.surface_tokens_peak, _surface_tokens(tools))

        builder_output = turn["tool_result_builder"](tool_results)
        if isinstance(builder_output, list):
            messages.extend(builder_output)
        else:
            messages.append(builder_output)
    else:
        # Running out of steps after the model reached the right tool and the
        # call failed its assertion is still a step-cap exit, but "too_few:data"
        # is the actionable half of that and the reason worth keeping. Only a
        # run that never produced an answer is reported as the cap itself.
        if not st.attempted_tools:
            st.fail_reason = "step_cap"

    latency = round(time.time() - t0, 2)

    if is_negative(fixture) or st.execution is None:
        # Nothing was executed, so the old rule is the only one available: a
        # negative fixture passes by declining, and a fixture whose model never
        # answered has nothing to assert on.
        passed = is_correct(st.selected_tool, fixture, st.fail_reason)
    else:
        # The call has to have run and satisfied the fixture's expectation, not
        # merely been named. `resolved` covers recovery: the model may have got
        # there on a later attempt.
        passed = st.resolved
    if not passed and st.fail_reason is None:
        st.fail_reason = "wrong_tool"
    if passed and st.fail_reason == "no_tool_call":
        # Declining IS the pass on a negative fixture. Leaving the reason set
        # would render a passing fixture with a failure reason attached.
        st.fail_reason = None

    return st.to_result(fixture, passed=passed, latency=latency)


def _handle_answering_call(
    st: DiscoveryState,
    call: ToolCall,
    fixture: Fixture,
    terminal_tools: set[str],
    session_id: str,
    endpoint: Endpoint,
    tool_results: list[tuple[str, str]],
) -> bool:
    """One answering (non-meta) call. Mutates `st` and returns whether the loop
    should stop — an unsafe tool or a correct-and-passing call both end the run,
    while a wrong or badly-called tool hands the server's words back and lets the
    model recover, so the caller continues.
    """
    correct = call["name"] in terminal_tools
    if st.selected_tool is None:
        # The first answer is what the old first-try metric measured; keep it
        # under the same name so historical reports and the per-tool scorecard
        # still mean what they meant.
        st.selected_tool, st.selected_input = call["name"], call["input"]
        st.first_tool_correct = correct

    attempt = AttemptRecord(tool=call["name"], correct=correct, step=st.steps)
    args, forced = prepare_call(call["name"], call["input"])
    st.dry_run_forced = st.dry_run_forced or forced

    if not is_executable(call["name"]):
        # Nothing safe to do with it — no dryRun to hide behind, or a tool the
        # snapshot has never seen. Graded on selection alone, which is where the
        # whole suite used to be.
        attempt["executed"] = False
        st.attempted_tools.append(attempt)
        st.execution = "skipped_unsafe" if classify(call["name"]) else "skipped_unclassified"
        if not correct:
            st.fail_reason = "wrong_tool"
        st.resolved = correct
        st.stop = True
        return True

    resp = mcp_call(session_id, call["name"], args, endpoint=endpoint)
    err = mcp_call_error(resp)
    result_text = mcp_result_text(resp) or ""
    st.payload_bytes += len(result_text.encode("utf-8"))
    st.execution = "executed"

    ok, reason = check(fixture.get("expect_result"), result_text, err)
    # The server's own words, truncated. Without this the report could say a call
    # failed but never what the server said, so diagnosing it meant re-running
    # with a debugger attached.
    #
    # `err or inband_error(...)` for the same reason check() folds the two
    # together: the admin merchant/entity tools answer a rejected call with
    # `isError: false` and `{"success": false, ...}` in the body, so `err` is
    # empty and only the in-band message exists. Recording just `err` is why
    # every failed attempt in the last run read `tool_error` with error="" —
    # the five gating failures had to be diagnosed from the fixture text.
    attempt["executed"] = True
    attempt["ok"] = ok
    attempt["reason"] = reason
    attempt["error"] = (err or inband_error(result_text) or "")[:200]
    st.attempted_tools.append(attempt)

    if correct and ok:
        st.resolved = True
        st.steps_to_correct = st.steps
        st.stop = True
        return True

    # Wrong tool, or the right tool called badly. Hand back what the server
    # actually said and let the model correct itself — that recovery is the thing
    # being measured, so no hint is injected.
    st.fail_reason = reason if correct else "wrong_tool"
    tool_results.append((call["id"], result_text or f"Error: {err}"))
    return False


# ---------------------------------------------------------------------------
# Providers
#
# One registry entry per provider, so adding a fifth is a single definition
# rather than an edit in tools_fn/turn_fn selection, two system-prompt branches,
# build_client, resolve_model and require_credentials. That scattering is what
# let the system-prompt check drift from `== "anthropic"` to `== "openai"` in
# one place only, silently running the github arm prompt-less while its report
# claimed otherwise.
# ---------------------------------------------------------------------------


# The SDK client per provider, credential value in. Lazily imported so a run
# with one provider does not require the other's package, and reading the base
# URL globals at call time so a test that overrides them is honoured.
def anthropic_client(key: str) -> object:
    import anthropic

    return anthropic.Anthropic(api_key=key)


def openai_client(key: str) -> object:
    from openai import OpenAI

    return OpenAI(api_key=key, base_url=None)


def github_client(key: str) -> object:
    # GitHub Models speaks the OpenAI wire format, so the same adapter and turn
    # function work — only the base URL and credential differ.
    from openai import OpenAI

    return OpenAI(api_key=key, base_url=GITHUB_MODELS_BASE_URL)


def lmstudio_client(key: str) -> object:
    from openai import OpenAI

    return OpenAI(api_key=key, base_url=LMSTUDIO_BASE_URL)


@dataclass(frozen=True)
class Provider:
    """One model provider. The three per-call variations — tool-schema shape,
    turn implementation, and where the context prompt goes — live here as one
    object instead of `if provider == ...` in three functions.

    The adapter/turn/client/dynamic-model callables are held by NAME and resolved
    through this module's globals at call time. That decouples the registry from
    definition order (turn functions are near the top, lmstudio_model at the
    bottom) and, importantly, keeps late binding: the adapter test suite
    monkeypatches `openai_turn`/`anthropic_turn` and expects the loop to pick the
    replacement up.
    """

    name: str
    default_model: str
    # Doubles as the module-global that holds the credential value, which
    # require_credentials reads (and tests override with setattr).
    credential_env: str
    # Anthropic takes the context prompt as a top-level `system` parameter; every
    # OpenAI-compatible provider carries it as a system message. This one bool is
    # what the drifted `== "openai"` check should always have been.
    system_as_param: bool
    tools_attr: str
    turn_attr: str
    client_attr: str
    # Set only for providers whose model is discovered at runtime (LM Studio
    # serves whatever is loaded and ignores the requested name).
    dynamic_model_attr: str | None = None

    def _resolve(self, attr: str) -> Callable[..., object]:
        # Held by name and looked up here, which is what keeps late binding: the
        # adapter tests monkeypatch `openai_turn`/`anthropic_turn` on the module
        # and expect the loop to pick the replacement up.
        return cast(Callable[..., object], globals()[attr])

    def tools_for(self, mcp_tools: list[ToolDef]) -> list[JsonObject]:
        return cast(list[JsonObject], self._resolve(self.tools_attr)(mcp_tools))

    def turn(
        self, client: object, model: str, system_prompt: str | None, messages: list[JsonObject], tools: list[JsonObject]
    ) -> Turn:
        return cast(Turn, self._resolve(self.turn_attr)(client, model, system_prompt, messages, tools))

    def build_client(self, credential_value: str) -> object:
        return self._resolve(self.client_attr)(credential_value)

    def resolve_model(self, requested: str | None) -> str:
        if explicit := (requested or os.environ.get("EVAL_MODEL")):
            return explicit
        if self.dynamic_model_attr:
            return str(self._resolve(self.dynamic_model_attr)())
        return self.default_model


PROVIDERS: dict[str, Provider] = {
    # Kept as the documented cross-vendor option, but no ANTHROPIC_API_KEY is
    # provisioned anywhere (CI runs OpenAI + GitHub Models), so nothing exercises
    # it today. It is now a single registry entry: if it stays unused, dropping
    # it is deleting this one line, anthropic_client, the two anthropic_* adapter
    # functions, and its pricing.yaml row — without touching the loop.
    "anthropic": Provider(
        name="anthropic",
        default_model="claude-sonnet-4-6",
        credential_env="ANTHROPIC_API_KEY",
        system_as_param=True,
        tools_attr="tools_for_anthropic",
        turn_attr="anthropic_turn",
        client_attr="anthropic_client",
    ),
    # This default is what CI resolves the PRIMARY eval to — the workflow's
    # `eval_model` input defaults to empty, so changing it changes the gating
    # model. It used to be gpt-4o, which made the primary and the gpt-4o-mini
    # second validator two variants of one model: same vendor, same generation,
    # same function-calling stack, so they tended to fail for the same reasons
    # and the both-fail bucket carried little independent signal. gpt-5.4-mini is
    # a generation removed from gpt-4o-mini while being cheaper than gpt-4o
    # ($0.75 vs ~$2.50 per 1M input) at the same latency; measured on the 24
    # disambiguation fixtures it scored 19/19 against gpt-4o's 18/19.
    "openai": Provider(
        name="openai",
        default_model="gpt-5.4-mini",
        credential_env="OPENAI_API_KEY",
        system_as_param=False,
        tools_attr="tools_for_openai",
        turn_attr="openai_turn",
        client_attr="openai_client",
    ),
    # A non-OpenAI publisher on purpose: as the second validator its value is
    # being an independent implementation, so it catches tool-description problems
    # specific to one vendor's function-calling behaviour.
    "github": Provider(
        name="github",
        default_model="mistral-ai/mistral-medium-2505",
        credential_env="GITHUB_TOKEN",
        system_as_param=False,
        tools_attr="tools_for_openai",
        turn_attr="openai_turn",
        client_attr="github_client",
    ),
    # A local OpenAI-compatible server for validating the harness for free before
    # CI. Its model is resolved at runtime (see lmstudio_model): the server serves
    # whatever is loaded and ignores the requested name, so default_model is only
    # the fallback label, which pricing.yaml prices at zero.
    "lmstudio": Provider(
        name="lmstudio",
        default_model="local-model",
        credential_env="LMSTUDIO_API_KEY",
        system_as_param=False,
        tools_attr="tools_for_openai",
        turn_attr="openai_turn",
        client_attr="lmstudio_client",
        dynamic_model_attr="lmstudio_model",
    ),
}

# Derived so tests and pricing that index by provider name keep one source of
# truth; the CLI's --provider choices come from the same registry.
PROVIDER_DEFAULTS = {name: p.default_model for name, p in PROVIDERS.items()}


def write_summary_row(
    provider: str,
    model: str,
    discovery: list[FixtureResult] | None,
    rate: float,
    ok: bool,
    args: argparse.Namespace,
) -> SummaryRow:
    """Record this run's verdict as a JSON row for the consolidated job summary.

    Reading a run's outcome from the log alone doesn't work: the tail is
    entirely post-job cleanup, and for an advisory (continue-on-error) step the
    reported conclusion is 'success' even when it failed, so the step status
    can't be trusted either.

    This used to append its own markdown table straight to GITHUB_STEP_SUMMARY.
    Three eval processes doing that produced three one-row tables, each
    re-printing the header, with the cross-model comparison wedged between rows
    two and three — and no way to tell which suite the third belonged to.
    Markdown appended from separate processes can't be made into one table, so
    the row is emitted as data and `eval/summary.py` renders them together at
    the end of the job.

    Returns the row (for tests); writes it only when --summary-row is set.
    """
    # `or []` throughout: a caller with no results at all must still get a row,
    # because the row is how the job summary learns the suite ran.
    graded = discovery or []
    throttled = count_rate_limited(graded)
    row: SummaryRow = {
        "suite": cast(str, args.suite_label) or cast(str, args.endpoint),
        "provider": provider,
        "model": model,
        "rate": rate,
        "graded": len(graded or []),
        "errored": sum(1 for r in graded or [] if r.get("error")),
        "throttled": throttled,
        "gate": "PASS" if ok else "FAIL",
        "advisory": bool(cast(bool, args.advisory)),
        # Per-owning-repo split, so the summary can say whether a failure landed
        # in core or in an optional plugin. One aggregate rate cannot. Built
        # over executed() — the same exclusions as the overall rate, so the
        # per-tier numbers stay comparable with it.
        "by_tier": breakdown(executed(graded)),
        # The row is what the consolidated summary renders from, so the cost has
        # to travel with it — otherwise the summary would have to re-read every
        # full report just to add one column.
        "cost": run_cost(graded, model, load_pricing(), provider),
    }

    # Also on stdout: the job summary now only appears once every eval has run,
    # so a timed-out or cancelled job would otherwise show nothing at all.
    print(
        f"\nSummary row: {row['suite']} | {provider} {model} | {round(rate * 100)}% | "
        f"graded={row['graded']} errors={row['errored']} throttled={throttled} | {row['gate']}"
    )

    if summary_row := cast(str | None, args.summary_row):
        path = Path(summary_row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, indent=2))

    if throttled:
        print(f"\n::warning::{throttled} fixture(s) hit provider rate limits — results are understated.")
    return row


def skipped_result(
    fixture: Fixture, mode: str, reason: str = "expected tool not registered on this instance"
) -> FixtureResult:
    """A fixture we decline to run, with the reason recorded.

    Two causes today, and they are different facts:

      * the expected tool is not registered on this instance — e.g. a dev-tools
        fixture on an instance without the SwagMcpDevTools bundle;
      * the tool is registered but the static layer proved it does not work, so
        grading a model on finding it would charge a plugin bug to the model.

    Either way it is skipped rather than failed, and excluded from scoring. The
    reason travels into the report because a shrinking denominator has to be
    explainable — that is the difference between a suite that scopes itself
    honestly and one that quietly stops testing things.
    """
    record: FixtureResult = {
        "schema_version": SCHEMA_VERSION,
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": mode,
        "prompt": fixture["prompt"],
        "expected_tool": fixture.get("expected_tool"),
        "selected_tool": None,
        "passed": False,
        "skipped": True,
        "skip_reason": reason,
    }
    return record


def load_tool_health(path: str | None) -> dict[str, ToolHealth]:
    """The static layer's per-tool verdict, or an empty map.

    Absent by design rather than by accident: a local run without the functional
    suite should still grade everything, so a missing file means "no evidence",
    not "nothing works". An unreadable one is reported and treated the same way —
    the gate must never be the thing that stops a run.
    """
    if not path:
        return {}
    try:
        health = as_object(cast(object, json.loads(Path(path).read_text())))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::could not read tool health from {path}: {exc}")
        return {}
    return {name: cast(ToolHealth, cast(object, as_object(entry))) for name, entry in health.items()}


def unhealthy_reason(tool: str | None, health: dict[str, ToolHealth]) -> str:
    """Why this tool's fixtures should not be graded, or '' if they should.

    Only `fail` blocks. A tool the static layer skipped is *unproven*, not
    broken — it may be unsafe to call, or its journey step never ran — and
    withholding its fixtures on that basis would shrink the suite every time the
    static layer got more cautious.
    """
    entry: ToolHealth = health.get(tool or "") or {"status": ""}
    if entry.get("status") != "fail":
        return ""
    return f"static checks failed for this tool: {entry.get('reason', 'no reason recorded')}"


def error_result(fixture: Fixture, mode: str, exc: Exception) -> FixtureResult:
    """Uniform failure record for a fixture that raised."""
    record: FixtureResult = {
        "schema_version": SCHEMA_VERSION,
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": mode,
        "prompt": fixture["prompt"],
        "expected_tool": fixture.get("expected_tool"),
        "selected_tool": None,
        "passed": False,
        "error": str(exc),
    }
    if mode == "discovery":
        # These keys are not decoration: an errored fixture is dropped from the
        # pass rate but stays in `scored()`, so discovery_summary reads them
        # with bracket access and a missing one is a KeyError mid-report.
        record.update(
            steps=0,
            meta_calls=[],
            discovery_path="none",
            search_hit=None,
            search_rank=None,
            search_score=None,
            search_candidates=None,
            enabled_correct_toolset=None,
        )
    return record


def run_fixtures_concurrently(
    fixtures: list[Fixture], worker: Callable[[Fixture], FixtureResult], workers: int
) -> list[FixtureResult]:
    """Run `worker(fixture)` over the fixtures with a bounded thread pool.

    Fixtures are independent (each discovery run opens its own MCP session), so
    they parallelize cleanly — the wall-clock win is large because almost all of
    the time is spent waiting on the LLM API. Results keep fixture order for the
    report; progress is printed as each one lands, which is why every line is
    prefixed with the fixture id.
    """
    results: list[FixtureResult | None] = [None] * len(fixtures)
    if workers <= 1:
        for index, fixture in enumerate(fixtures):
            results[index] = worker(fixture)
        return [r for r in results if r is not None]

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, fixture): index for index, fixture in enumerate(fixtures)}
        for future in as_completed(futures):
            # Bound to a local before indexing back into `results`, whose element
            # type is `dict | None` until every slot is filled.
            result = future.result()
            results[futures[future]] = result
            completed += 1
            print(f"  {DIM}[{completed:02d}/{len(fixtures):02d}]{RESET} {result.get('_line', '')}")
    return [r for r in results if r is not None]


def run_discovery_pass(
    provider: str,
    client: object,
    fixtures: list[Fixture],
    model: str,
    system_prompt: str | None,
    default_max_steps: int,
    available_tools: set[str],
    endpoint: Endpoint = ADMIN,
    workers: int = 1,
    tool_health: dict[str, ToolHealth] | None = None,
) -> list[FixtureResult]:
    tool_health = tool_health or {}
    print(f"\n{BOLD}── Mode: discovery (default surface + agentic loop) ──{RESET}\n")
    print(f"  concurrency={workers}\n")

    def worker(fixture: Fixture) -> FixtureResult:
        # A negative fixture names no tool, so there is nothing to be missing —
        # it always runs. (It is easier on an instance with fewer plugins, since
        # fewer tools exist to be wrongly picked; that is a caveat on comparing
        # negative rates across instances, not a reason to skip.)
        expected = fixture.get("expected_tool")
        if expected and expected not in available_tools:
            result = skipped_result(fixture, "discovery")
            result["_line"] = render_line(result)
            return result
        # The lane could not supply an id this prompt names, so the call the
        # model would make cannot resolve. Grading that charges the lane's gap
        # to the model — the three cart fixtures failed on exactly this while
        # the model named merchant-cart-checkout correctly every time.
        if unresolved := fixture.get("unresolved_placeholder"):
            result = skipped_result(fixture, "discovery", f"lane could not resolve {{{unresolved}}}")
            result["_line"] = render_line(result)
            return result
        # Registered but proven broken by the static layer. Grading a model on
        # finding a tool that cannot run charges a plugin bug to the model, and
        # pays full model price to learn something one direct call already
        # established.
        if reason := unhealthy_reason(expected, tool_health):
            result = skipped_result(fixture, "discovery", reason)
            result["_line"] = render_line(result)
            return result
        max_steps = int(fixture.get("max_steps", default_max_steps))
        try:
            result = run_fixture_discovery(
                provider, client, fixture, model, system_prompt, max_steps, endpoint=endpoint
            )
            attempts = 1
            # Retry once on failure: the models are nondeterministic, so a single
            # borderline miss shouldn't flip CI red. A real regression fails
            # both attempts. Skips/errors are not retried.
            if not result["passed"]:
                retry = run_fixture_discovery(
                    provider, client, fixture, model, system_prompt, max_steps, endpoint=endpoint
                )
                attempts = 2
                if retry["passed"]:
                    result = retry
            result["attempts"] = attempts
        except Exception as exc:  # noqa: BLE001 — recorded as a failed fixture
            result = error_result(fixture, "discovery", exc)
        result["_line"] = render_line(result)
        return result

    return run_fixtures_concurrently(fixtures, worker, workers)


# Categories that only make sense against the live discovery surface. `meta`
# fixtures expect a meta-tool, which the diagnostic arms withhold; `discovery`
# fixtures exist to exercise search and enablement, which the arms bypass; a
# `negative` fixture asks whether anything bites, and pre-enabling a group to
# ask that would be a different question.
ARM_SKIP_CATEGORIES = frozenset({"meta", "discovery", "negative"})


def triage_arms(
    provider: str,
    client: object,
    discovery_results: list[FixtureResult] | None,
    fixtures: list[Fixture],
    model: str,
    system_prompt: str | None,
    default_max_steps: int,
    endpoint: Endpoint = ADMIN,
    workers: int = 1,
    arms: tuple[str, ...] = ("isolated", "full"),
) -> dict[str, list[FixtureResult]]:
    """Re-run the discovery arm's failures under the diagnostic arms.

    Triage, not a full pass. Running every fixture through all three arms costs
    roughly three times as much and the extra two thirds is spent confirming
    that fixtures which already passed still pass — the matrix only says
    anything where discovery failed. On a ~10% failure rate this is ~18 extra
    runs instead of ~180.

    What it buys: a failure stops being "the model got it wrong" and becomes a
    location. Fails everywhere → the tool's own description. Passes isolated,
    fails full → a collision with something in another group. Passes both, fails
    discovery → the discovery layer itself, meaning the group description or
    tool-search ranking.
    """
    by_id = {f["id"]: f for f in fixtures}
    failed = [
        by_id[r["id"]]
        for r in executed(discovery_results or [])
        if not r["passed"] and r["id"] in by_id and by_id[r["id"]].get("category") not in ARM_SKIP_CATEGORIES
    ]
    if not failed:
        print(f"\n{BOLD}── Triage: no discovery failures to diagnose ──{RESET}\n")
        return {}

    out: dict[str, list[FixtureResult]] = {}
    for arm in arms:
        print(f"\n{BOLD}── Triage arm: {arm} ({len(failed)} discovery failures) ──{RESET}\n")

        def worker(fixture: Fixture, arm: str = arm) -> FixtureResult:
            try:
                result = run_fixture_discovery(
                    provider,
                    client,
                    fixture,
                    model,
                    system_prompt,
                    int(fixture.get("max_steps", default_max_steps)),
                    endpoint=endpoint,
                    arm=arm,
                )
            except Exception as exc:  # noqa: BLE001 — recorded as a failed fixture
                result = error_result(fixture, arm, exc)
            result["_line"] = render_line(result)
            return result

        out[arm] = run_fixtures_concurrently(failed, worker, workers)
    return out


class ConfigError(Exception):
    """A usage/configuration problem: bad mode, missing credential, no fixtures.

    Raised rather than calling sys.exit so each step below can be unit-tested
    without catching SystemExit. main() turns it into the exit code.
    """


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shopware MCP LLM Eval Runner (v2 discovery)")
    parser.add_argument(
        "--provider",
        choices=list(PROVIDERS),
        default=os.environ.get("EVAL_PROVIDER", "anthropic"),
        help=(
            "anthropic | openai | github (GitHub Models: free, OpenAI-compatible, auth via GITHUB_TOKEN) "
            "| lmstudio (a local OpenAI-compatible server; free, for validating the harness before CI)"
        ),
    )
    parser.add_argument("--model", default=None)
    # Kept as a flag rather than deleted so existing invocations and the docs'
    # `--modes discovery` keep working; `discovery` is now the only legal value.
    parser.add_argument("--modes", default="discovery", help="Only 'discovery' is supported (baseline was removed)")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Max assistant turns in discovery mode (per-fixture max_steps overrides)",
    )
    parser.add_argument("--no-system-prompt", action="store_true", help="Alias for --context-prompts none")
    parser.add_argument(
        "--context-prompts",
        choices=sorted(PROMPT_SETS),
        default="all",
        help=(
            "Which of the server's MCP context prompts to send, named after the installation each "
            "mirrors. Every area ships its own, and sending all of them to every fixture puts "
            "instructions naming another area's tools in front of the model. `all` is a fully "
            "installed shop; `core` is vanilla Shopware. Core is always included — it carries the "
            "discovery procedure, without which nothing is reachable."
        ),
    )
    parser.add_argument(
        "--tool-health",
        help=(
            "results/tool-health-<endpoint>.json from the functional suite. Fixtures whose expected "
            "tool it proved broken are skipped with that reason instead of graded, so a plugin bug "
            "does not read as a description problem and does not cost a model pass to rediscover. "
            "An absent file grades everything."
        ),
    )
    parser.add_argument(
        "--seed-lane",
        action="store_true",
        default=os.environ.get("EVAL_SEED_LANE") == "true",
        help=(
            "Allow the placeholder resolvers to WRITE to the shop — today, to open a cart with a "
            "line item in it so {cart_token} and {line_item_id} resolve. Only for a throwaway "
            "instance: CI sets it because the lane is destroyed with the job. Off, the fixtures "
            "that name those ids are skipped rather than graded against a token nothing can resolve."
        ),
    )
    parser.add_argument(
        "--triage",
        action="store_true",
        help=(
            "After the discovery pass, re-run ONLY its failures under the isolated and full "
            "arms, to locate each failure (own description / cross-group collision / discovery layer). "
            "Advisory: never affects the gate."
        ),
    )
    # Fixtures are independent and almost entirely LLM-API-bound, so running them
    # concurrently cuts wall-clock roughly linearly. This used to be capped at 4
    # because each discovery step also hits the MCP endpoint, which answered 429
    # once CI inherited the production mcp_admin_api limits; the workflow now
    # disables that limiter on its throwaway instance, so the cap is the model
    # provider's own rate limit and the server's worker pool instead.
    parser.add_argument(
        "--discovery-concurrency",
        type=int,
        default=int(os.environ.get("EVAL_DISCOVERY_CONCURRENCY", "4")),
        help=(
            "Parallel fixtures (default 4, which is what an instance with the MCP rate limiter "
            "still on can take; CI disables that limiter and sets EVAL_DISCOVERY_CONCURRENCY=12)"
        ),
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=float(os.environ.get("EVAL_MIN_PASS_RATE", "0.9")),
        help="Min gating-mode pass rate for exit 0, over fixtures that ran (default 0.9; 1.0 for strict)",
    )
    parser.add_argument(
        "--min-core-pass-rate",
        type=float,
        default=(float(os.environ["EVAL_MIN_CORE_PASS_RATE"]) if os.environ.get("EVAL_MIN_CORE_PASS_RATE") else None),
        help=(
            "Min pass rate for core (shopware/shopware) fixtures alone, on their own denominator, so a core "
            "regression cannot hide behind clean optional-plugin numbers. Defaults to --min-pass-rate."
        ),
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=float(os.environ.get("EVAL_MAX_ERROR_RATE", "0.1")),
        help=(
            "Max share of fixtures that may error (transport/provider failures) before the run is "
            "treated as invalid rather than scored (default 0.1)"
        ),
    )
    parser.add_argument(
        "--endpoint",
        choices=["admin", "store"],
        default="admin",
        help="Which MCP endpoint to test (default admin). 'store' uses the Store API + UCP tools.",
    )
    parser.add_argument(
        "--fixtures", help="Fixtures file (default: fixtures.yaml, or fixtures_store.yaml for --endpoint store)"
    )
    parser.add_argument("--category", help="Run only fixtures of this category")
    parser.add_argument("--id", help="Run only this fixture ID")
    parser.add_argument("--output", help="Path to save JSON report")
    parser.add_argument(
        "--summary-row",
        help=(
            "Path to write this run's one-row verdict as JSON, for eval/summary.py to render into the "
            "GitHub job summary. Omit for local runs."
        ),
    )
    parser.add_argument(
        "--suite-label",
        help=(
            "How this run is named in the job summary's run table (e.g. 'admin · primary'). "
            "The workflow knows which role a run plays; the script cannot infer it from --endpoint alone, "
            "since the primary and the second validator share one. Defaults to --endpoint."
        ),
    )
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Mark this run as non-gating in the job summary (e.g. the Store/UCP suite, which is continue-on-error)",
    )
    return parser


def parse_modes(spec: str) -> list[str]:
    modes = [m.strip() for m in spec.split(",") if m.strip()]
    if "baseline" in modes:
        raise ConfigError(
            "baseline mode was removed: it graded the first call without exempting the discovery "
            "meta-tools it had put in the catalogue, so following the server's own instructions "
            "scored as the wrong tool. Use --modes discovery."
        )
    unknown = [m for m in modes if m != "discovery"]
    if unknown:
        raise ConfigError(f"unknown mode(s): {', '.join(unknown)}")
    if not modes:
        raise ConfigError("no modes selected")
    return modes


def lmstudio_model() -> str:
    """The model LM Studio currently has loaded.

    Asked at runtime because a local server serves whatever is loaded and ignores
    the name in the request — so a hardcoded label would put "local-model" in the
    report where the reader needs "qwen/qwen3.6-35b-a3b". Falls back to the label
    if the server is unreachable; the run will fail immediately afterwards
    anyway, and failing here would hide why.
    """
    try:
        body = cast(object, requests.get(f"{LMSTUDIO_BASE_URL.rstrip('/')}/models", timeout=5).json())
    except (requests.RequestException, ValueError):
        return PROVIDER_DEFAULTS["lmstudio"]
    ids = [str(as_object(m).get("id", "")) for m in as_list(as_object(body).get("data"))]
    # Embedding models sit in the same list and cannot answer a chat request.
    chat = [i for i in ids if "embed" not in i]
    return chat[0] if chat else PROVIDER_DEFAULTS["lmstudio"]


def resolve_model(provider: str, requested: str | None) -> str:
    """CLI flag wins, then EVAL_MODEL, then the provider default.

    The default is what CI resolves the gating model to (see the openai entry in
    PROVIDERS), so changing it changes which model gates.
    """
    return PROVIDERS[provider].resolve_model(requested)


def require_credentials(provider: str, endpoint_name: str) -> tuple[str, str]:
    """Check the server and provider credentials this run needs.

    Returns the (name, value) of the provider credential, because build_client
    needs the value and the `github` provider's differs from OpenAI's. The value
    is read from this module's globals at call time so tests can override it with
    setattr — and so lmstudio's placeholder default is never empty, which would
    otherwise fail the check on a server that wants no credential at all.
    """
    required = [("SW_BASE_URL", SW_BASE_URL)]
    if endpoint_name == "store":
        required.append(("SW_SC_ACCESS_KEY", SW_SC_ACCESS_KEY))
    else:
        required += [("SW_ACCESS_KEY", SW_ACCESS_KEY), ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)]
    env = PROVIDERS[provider].credential_env
    credential = (env, str(cast(object, globals().get(env, ""))))
    required.append(credential)
    missing = [var for var, val in required if not val]
    if missing:
        raise ConfigError(f"{', '.join(missing)} is not set.")
    return credential


def build_client(provider: str, credential: tuple[str, str]) -> object:
    """The provider SDK client. Imported lazily so a run with one provider does
    not require the other's package to be installed."""
    return PROVIDERS[provider].build_client(credential[1])


def fixtures_path_for(endpoint_name: str, override: str | None) -> Path:
    if override:
        return Path(override)
    return Path(__file__).parent / ("fixtures_store.yaml" if endpoint_name == "store" else "fixtures.yaml")


def load_fixtures(path: Path, category: str | None = None, fixture_id: str | None = None) -> list[Fixture]:
    loaded = as_list(as_object(cast(object, yaml.safe_load(path.read_text()))).get("fixtures"))
    fixtures = [cast(Fixture, cast(object, as_object(f))) for f in loaded]
    if category:
        fixtures = [f for f in fixtures if f.get("category") == category]
    if fixture_id:
        fixtures = [f for f in fixtures if f["id"] == fixture_id]
    if not fixtures:
        raise ConfigError("No fixtures matched the filter.")
    return fixtures


def _first_sales_channel_id(endpoint: Endpoint) -> str | None:
    """A real sales-channel id off the live lane, preferring the demo
    `Storefront` channel.

    Why fixtures carry `{sales_channel_id}` and not a literal UUID: the id is
    per-instance (demo data is generated), so hardcoding one goes stale on every
    other lane. And why it has to be REAL rather than a plausible fake: grading
    now executes the call, and merchant-* / theme-config reject a channel *name*
    with `Value is not a valid UUID: Storefront`, so a fake id can never pass.
    Resolved once here, substituted into the prompt before the model sees it.
    """
    session_id, _ = mcp_init(endpoint=endpoint)
    resp = mcp_call(session_id, "shopware-entity-search", {"entity": "sales_channel", "limit": 25}, endpoint=endpoint)
    try:
        body = as_object(cast(object, json.loads(mcp_result_text(resp) or "")))
    except (json.JSONDecodeError, TypeError):
        return None
    raw = body.get("data") or body.get("elements")
    keyed = as_object(raw)
    rows = [as_object(r) for r in (list(keyed.values()) if keyed else as_list(raw))]

    def _name(r: JsonObject) -> str:
        return str(r.get("name") or as_object(r.get("translated")).get("name") or "")

    # Storefront first, then any channel with an id — the tool only needs a
    # resolvable one; the name match is for the prompt reading coherently.
    for row in sorted(rows, key=lambda r: _name(r).lower() != "storefront"):
        if row_id := row.get("id"):
            return str(row_id)
    return None


def _first_id_of(entity: str) -> LaneResolver:
    """A resolver that returns the id of any one row of `entity`.

    Any row, not a specific one: what these fixtures need is an id the server
    can resolve, so the call it grades is a real call. Which product it is
    carries no information — the demo data is generated, so there is nothing
    stable to prefer.
    """

    def resolve(endpoint: Endpoint) -> str | None:
        session_id, _ = mcp_init(endpoint=endpoint)
        return lane.first_entity_id(session_id, endpoint, entity) or None

    resolve.__name__ = f"_first_{entity}_id"
    return resolve


def _seed_cart(endpoint: Endpoint) -> dict[str, str]:
    """MUTATES: a real cart with a real line item, as {cart_token, line_item_id}.

    One resolver for both ids because they come from one cart. Two independent
    ones would open two, and `{line_item_id}` would name a line in a cart that
    `{cart_token}` does not point at.

    This is the only resolver that writes, which is why it is reached through
    SEEDING_RESOLVERS and only when --seed-lane says the instance is disposable.
    A cart cannot be looked up: nothing in a fresh shop has one, and the three
    cart fixtures were failing against a token invented in the YAML — the model
    picked merchant-cart-checkout correctly all three times and was marked wrong
    for it.
    """
    session_id, _ = mcp_init(endpoint=endpoint)
    channel = _first_sales_channel_id(endpoint) or ""
    products = lane.sellable_products(session_id, endpoint, channel)
    token, line_item_id = lane.create_cart(session_id, endpoint, channel, products)
    return {"cart_token": token, "line_item_id": line_item_id}


# Placeholder -> resolver. A fixture prompt may contain `{name}`; the runner
# replaces it with a value read off the live lane at startup. Add one here when a
# tool needs a real, resolvable id to execute that the model cannot invent.
#
# Everything here READS. `{product_id}` and friends go through core
# entity-search rather than the merchant tools so they still resolve on an
# instance with no plugins installed.
PLACEHOLDER_RESOLVERS: dict[str, LaneResolver] = {
    "sales_channel_id": _first_sales_channel_id,
    "product_id": _first_id_of("product"),
    "customer_id": _first_id_of("customer"),
    "order_id": _first_id_of("order"),
}

# Resolvers that WRITE to the shop, keyed by the placeholders each one provides.
# Held apart from the read-only set and reached only under --seed-lane: creating
# a cart on somebody's real instance to grade a fixture is not a trade this
# suite gets to make on its own.
SEEDING_RESOLVERS: dict[tuple[str, ...], SeedingResolver] = {
    ("cart_token", "line_item_id"): _seed_cart,
}

# Every placeholder this runner knows how to fill. Used to tell an unresolved
# `{cart_token}` (a lane that could not provide one) apart from a stray brace in
# a prompt, which is nobody's business but the fixture author's.
KNOWN_PLACEHOLDERS = set(PLACEHOLDER_RESOLVERS) | {k for keys in SEEDING_RESOLVERS for k in keys}


def _referenced(fixtures: list[Fixture], key: str) -> bool:
    return any("{" + key + "}" in f.get("prompt", "") for f in fixtures)


# A resolver talks to the server, so it can fail the way any network call fails.
# requests raises ConnectionError (an OSError), mcp_init raises RuntimeError for a
# protocol problem, and a malformed body surfaces as ValueError.
LANE_LOOKUP_ERRORS = (OSError, RuntimeError, ValueError)


def _lane_lookup_failed(key: str, exc: Exception) -> None:
    print(f"::warning::resolving {{{key}}} off the lane failed ({type(exc).__name__}: {exc})")


# Two wrappers rather than one returning `str | dict`. The union needed a cast or
# an isinstance at both call sites to say something each caller already knows:
# a read-only resolver yields one id, a seeding resolver yields the several it
# filled from one cart. Splitting them keeps each precisely typed.
#
# Both swallow, because a failed lookup must not be the thing that ends a run.
# The unresolved placeholder already has a defined meaning — the fixtures naming
# it are skipped — so degrading to that is strictly better than dying at startup
# before a single fixture has been graded, which is the exact failure this file's
# own regression test was written for.
def _resolve_one(key: str, resolver: LaneResolver, endpoint: Endpoint) -> str | None:
    try:
        return resolver(endpoint)
    except LANE_LOOKUP_ERRORS as exc:
        _lane_lookup_failed(key, exc)
        return None


def _resolve_many(key: str, resolver: SeedingResolver, endpoint: Endpoint) -> dict[str, str]:
    try:
        return resolver(endpoint)
    except LANE_LOOKUP_ERRORS as exc:
        _lane_lookup_failed(key, exc)
        return {}


def resolve_lane_substitutions(fixtures: list[Fixture], endpoint: Endpoint, seed_lane: bool = False) -> dict[str, str]:
    """Values for every `{placeholder}` the loaded fixtures actually reference.

    Only resolves what is used, so a run filtered to fixtures with no placeholder
    makes no extra calls. A placeholder that cannot be resolved is warned about
    and left in place — the fixture is then skipped by name rather than silently
    graded against a literal `{sales_channel_id}`.
    """
    subs: dict[str, str] = {}
    for key, resolver in PLACEHOLDER_RESOLVERS.items():
        if not _referenced(fixtures, key):
            continue
        value = _resolve_one(key, resolver, endpoint)
        if value:
            subs[key] = value
            print(f"Lane id: {key} = {value}")
        else:
            print(f"::warning::could not resolve {{{key}}} from the lane; fixtures using it will be skipped")

    for keys, resolver in SEEDING_RESOLVERS.items():
        wanted = [k for k in keys if _referenced(fixtures, k)]
        if not wanted:
            continue
        if not seed_lane:
            print(
                f"Lane seeding off (--seed-lane): {', '.join(wanted)} cannot be resolved without writing "
                "to the shop, so their fixtures are skipped."
            )
            continue
        for key, value in _resolve_many("/".join(wanted), resolver, endpoint).items():
            if value:
                subs[key] = value
                print(f"Lane id (seeded): {key} = {value}")
    for key in (k for keys in SEEDING_RESOLVERS for k in keys):
        if seed_lane and _referenced(fixtures, key) and key not in subs:
            print(f"::warning::could not seed {{{key}}} on this lane; fixtures using it will be skipped")
    return subs


def apply_substitutions(fixtures: list[Fixture], subs: dict[str, str]) -> None:
    """Replace `{placeholder}` tokens in each fixture prompt, in place.

    A fixture left holding a placeholder this runner knows about is marked
    `unresolved_placeholder` rather than sent to the model. Grading it would
    charge the model for an id the lane could not supply — which is exactly the
    failure mode the placeholders exist to remove.
    """
    for fixture in fixtures:
        prompt = fixture.get("prompt", "")
        for key, value in subs.items():
            prompt = prompt.replace("{" + key + "}", value)
        fixture["prompt"] = prompt
        missing = [k for k in KNOWN_PLACEHOLDERS if "{" + k + "}" in prompt]
        if missing:
            fixture["unresolved_placeholder"] = ", ".join(sorted(missing))


def fetch_system_prompt(
    endpoint: Endpoint, enabled: bool = True, prompt_set: str = "all"
) -> tuple[str | None, PromptInventory]:
    """The server's instructions plus its context prompts, and what they were.

    Returns (prompt, inventory). The inventory travels into the report because a
    boolean cannot answer the question that matters: admin serves four prompts
    totalling ~20k characters and store serves none, so the two endpoints' pass
    rates were never comparable and nothing recorded why.
    """
    if not enabled or prompt_set == "none":
        print("Context prompt: none")
        return None, PromptInventory(names=[], chars={}, total_chars=0, set="none", disabled=True)
    session_id, server_instructions = mcp_init(endpoint=endpoint)
    prompt, inventory = mcp_fetch_context_prompts(
        session_id, server_instructions, endpoint=endpoint, owners=PROMPT_SETS[prompt_set]
    )
    inventory["set"] = prompt_set
    named = ", ".join(inventory["names"]) or "none"
    chars = inventory.get("total_chars", 0)
    print(f"Context prompt [{prompt_set}]: {chars} chars from {len(inventory['names'])}: {named}")
    if excluded := inventory.get("excluded"):
        print(f"  withheld ({len(excluded)}): {', '.join(excluded)}")
    return prompt, inventory


def probe_catalogue(endpoint: Endpoint) -> set[str]:
    """Every tool registered on this instance, with all toolsets enabled.

    Fixtures whose expected tool is absent (a plugin bundle that isn't installed)
    are skipped rather than scored as failures.
    """
    session_id, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(session_id, endpoint=endpoint)
    return {t["name"] for t in mcp_tools_list_all(session_id, endpoint=endpoint)}


def build_report(
    provider: str,
    model: str,
    fixtures: list[Fixture],
    discovery: list[FixtureResult] | None,
    system_prompt_enabled: bool,
    max_steps: int,
    arm_results: dict[str, list[FixtureResult]] | None = None,
    prompt_inventory: PromptInventory | None = None,
) -> Report:
    """The JSON report. Pure: no writing, so its shape can be asserted directly."""
    report: Report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "server": SW_BASE_URL,
        "provider": provider,
        "model": model,
        "modes": {},
        "fixtures": len(fixtures),
        "system_prompt": system_prompt_enabled,
        # The inventory, not just the flag: two runs with the same boolean can
        # have had different prompts, and the admin/store gap is invisible
        # without it.
        "context_prompt": cast(JsonObject, cast(object, prompt_inventory or {})),
        "max_steps": max_steps,
    }
    if discovery is not None:
        report["modes"]["discovery"] = ModeBlock(
            passed=sum(1 for r in scored(discovery) if r["passed"]),
            failed=sum(1 for r in scored(discovery) if not r["passed"]),
            skipped=sum(1 for r in discovery if r.get("skipped")),
            results=discovery,
        )
        report["discovery_summary"] = cast(JsonObject, cast(object, discovery_summary(discovery)))
        # What was NOT graded, and why. A pass rate over a denominator that
        # silently shrank is the failure mode this guards: the number goes up
        # because the hard cases stopped being asked, and nothing says so.
        report["skipped_fixtures"] = [
            SkippedFixture(id=r["id"], expected_tool=r.get("expected_tool"), reason=r.get("skip_reason", ""))
            for r in discovery
            if r.get("skipped")
        ]
    # Diagnostic arms sit alongside discovery under the same key, so
    # compare_runs and the gate — which both read modes["discovery"] by name —
    # are untouched by their presence.
    for arm, records in (arm_results or {}).items():
        report["modes"][arm] = ModeBlock(
            passed=sum(1 for r in scored(records) if r["passed"]),
            failed=sum(1 for r in scored(records) if not r["passed"]),
            skipped=sum(1 for r in records if r.get("skipped")),
            results=records,
        )
    # Per-owning-repo rates, so the report answers "which codebase regressed"
    # without re-deriving attribution downstream. `or []` covers discovery not
    # having run — parse_modes rejects an empty mode list, so that is unreachable
    # via the CLI, but an empty table is the honest answer rather than a crash for
    # a direct caller.
    report["by_tier"] = breakdown(executed(discovery or []))
    # What this run cost, in dollars and in the volume behind them. Recorded in
    # the report rather than only printed so cost_drift.py can compare a run
    # against its predecessor without re-deriving anything.
    report["cost"] = run_cost(discovery or [], model, load_pricing(), provider)
    return report


def print_gate(verdict: GateVerdict, args: argparse.Namespace) -> None:
    """The gate block. Reads only the verdict dict, so gate_verdict stays the
    single place the pass/fail decision is made."""
    gating, graded = verdict["gating"], verdict["graded"]
    print(
        f"\nGate: {verdict['passed']}/{len(gating)} = {round(verdict['rate'] * 100)}% "
        f"(threshold {round(cast(float, args.min_pass_rate) * 100)}%) → {'PASS' if verdict['quality_ok'] else 'FAIL'}"
    )
    print_tier_block(gating)
    if verdict["core_total"]:
        print(
            f"  Core gate: {verdict['core_passed']}/{verdict['core_total']} = "
            f"{round(verdict['core_rate'] * 100)}% (threshold {round(verdict['min_core'] * 100)}%) → "
            f"{'PASS' if verdict['core_ok'] else 'FAIL'}"
        )
    if verdict["errored"]:
        print(
            f"  {verdict['errored']}/{len(graded)} fixtures never reached the model "
            f"({round(verdict['error_rate'] * 100)}%, budget {round(cast(float, args.max_error_rate) * 100)}%) → "
            f"{'within budget' if verdict['run_valid'] else 'RUN INVALID'}"
        )
    if not verdict["quality_ok"]:
        print(f"  below threshold; failing: {', '.join(r['id'] for r in gating if not r['passed'])}")
    if not verdict["core_ok"]:
        core_failed = [r["id"] for r in gating if not r["passed"] and owner_of(r.get("expected_tool", "")) == CORE]
        print(f"  {RED}core below threshold{RESET}; failing: {', '.join(core_failed)}")
    if not verdict["run_valid"]:
        print("  too many fixtures errored to trust this run — fix the server/provider, then re-run.")


def run_suite(args: argparse.Namespace) -> int:
    """One eval run end to end. Returns the process exit code."""
    provider = cast(str, args.provider)
    endpoint_name = cast(str, args.endpoint)
    max_steps = cast(int, args.max_steps)
    concurrency = cast(int, args.discovery_concurrency)
    no_system_prompt = cast(bool, args.no_system_prompt)
    model = resolve_model(provider, cast(str | None, args.model))
    modes = parse_modes(cast(str, args.modes))
    endpoint = endpoint_by_name(endpoint_name)
    credential = require_credentials(provider, endpoint_name)
    client = build_client(provider, credential)
    fixtures = load_fixtures(
        fixtures_path_for(endpoint_name, cast(str | None, args.fixtures)),
        cast(str | None, args.category),
        cast(str | None, args.id),
    )

    print(f"{BOLD}Shopware MCP LLM Eval (v2 discovery){RESET}")
    print(f"Server:   {SW_BASE_URL}  ({endpoint.name} endpoint)")
    print(f"Provider: {provider}")
    print(f"Model:    {model}")
    print(f"Modes:    {', '.join(modes)}")
    print(f"Fixtures: {len(fixtures)}")

    print("\nInitializing MCP session for system prompt...")
    system_prompt, prompt_inventory = fetch_system_prompt(
        endpoint, enabled=not no_system_prompt, prompt_set=cast(str, args.context_prompts)
    )

    available_tools = probe_catalogue(endpoint)
    # `.get()`: a negative fixture names no tool, so there is nothing that could
    # be absent from the catalogue — and indexing it here crashed the whole run
    # before a single fixture had been graded.
    absent = sorted({t for f in fixtures if (t := f.get("expected_tool"))} - available_tools)
    if absent:
        print(f"Catalogue: {len(available_tools)} tools; will skip fixtures for absent: {', '.join(absent)}")

    tool_health = load_tool_health(cast(str | None, args.tool_health))
    blocked = sorted({t for f in fixtures if (t := f.get("expected_tool")) and unhealthy_reason(t, tool_health)})
    if blocked:
        print(f"Tool health: skipping fixtures for {len(blocked)} broken tool(s): {', '.join(blocked)}")

    # Substitute {placeholder} tokens with real ids off the live lane. A tool
    # whose call is executed (merchant-*, theme-config, entity-upsert) needs
    # resolvable ids, which the model cannot invent — see
    # resolve_lane_substitutions. Anything left unresolved skips its fixtures
    # rather than grading them against a literal brace string.
    apply_substitutions(fixtures, resolve_lane_substitutions(fixtures, endpoint, seed_lane=cast(bool, args.seed_lane)))

    results_discovery: list[FixtureResult] | None = None
    if "discovery" in modes:
        results_discovery = run_discovery_pass(
            provider,
            client,
            fixtures,
            model,
            system_prompt,
            max_steps,
            available_tools,
            endpoint=endpoint,
            workers=concurrency,
            tool_health=tool_health,
        )

    # `_line` is progress-display scaffolding, not part of the report contract.
    for result in results_discovery or []:
        result.pop("_line", None)

    if results_discovery:
        print_single_mode(results_discovery, "discovery")
        print_discovery_block(results_discovery)

    arm_results: dict[str, list[FixtureResult]] = {}
    if cast(bool, args.triage):
        arm_results = triage_arms(
            provider,
            client,
            results_discovery,
            fixtures,
            model,
            system_prompt,
            max_steps,
            endpoint=endpoint,
            workers=concurrency,
        )
        for records in arm_results.values():
            for record in records:
                record.pop("_line", None)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_path = Path(cast(str | None, args.output) or BASE / "results" / f"eval-{provider}-{ts}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_report(
                provider,
                model,
                fixtures,
                results_discovery,
                not no_system_prompt,
                max_steps,
                arm_results,
                prompt_inventory,
            ),
            indent=2,
        )
    )
    print(f"Report saved: {output_path}")

    # Gate: skipped fixtures (tool absent on this instance) do not gate.
    #
    # The LLM eval is a quality signal against a nondeterministic model, so it
    # gates on a pass-rate threshold rather than a strict 100% — a couple of
    # borderline fixtures shouldn't flip CI red, but a real regression (the rate
    # collapsing) still fails. Each failed fixture is also retried once (see
    # run_discovery_pass). Set --min-pass-rate 1.0 for strict.
    verdict = gate_verdict(
        results_discovery or [],
        min_pass_rate=cast(float, args.min_pass_rate),
        min_core_pass_rate=cast(float | None, args.min_core_pass_rate),
        max_error_rate=cast(float, args.max_error_rate),
    )
    print_gate(verdict, args)
    write_summary_row(provider, model, results_discovery, verdict["rate"], verdict["ok"], args)
    return 0 if verdict["ok"] else 1


# Exit codes, because the workflow treats them differently. 1 means the run
# happened and the gate said no, which an advisory window is allowed to downgrade
# to a warning — catalogue drift is the one that remains, since a verdict about
# descriptions that are not the ones the baseline recorded is not attributable.
# CRASH_EXIT means the run produced no verdict at all, and no window may swallow
# that: a green job that actually crashed is worse than a red one.
CRASH_EXIT = 3


def main() -> int:
    try:
        return run_suite(build_parser().parse_args())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 — re-raised as a distinct exit code, traceback intact
        traceback.print_exc()
        print(
            "\nERROR: the run crashed before producing a verdict. This is not a gate "
            "failure and is not downgraded by any advisory window.",
            file=sys.stderr,
        )
        return CRASH_EXIT


if __name__ == "__main__":
    sys.exit(main())
