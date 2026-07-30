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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import yaml

from eval.assertions import check
from eval.cost import load_pricing, run_cost
from eval.report import (
    BOLD,
    DIM,
    RED,
    RESET,
    _render,
    print_discovery_block,
    print_single_mode,
    print_tier_block,
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
    enable_all_toolsets,
    enable_toolset,
    endpoint_by_name,
    mcp_call,
    mcp_call_error,
    mcp_fetch_system_prompt,
    mcp_init,
    mcp_result_text,
    mcp_tools_list_all,
)
from ownership import CORE, breakdown, owner_of
from toolclass import classify, is_executable, prepare_call

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# GitHub Models: an OpenAI-compatible endpoint authenticated with the workflow's
# built-in GITHUB_TOKEN (needs `models: read`). It costs nothing and carries
# non-OpenAI publishers, so it gives a cross-provider second opinion without
# provisioning an API key. Its catalogue has no Anthropic models.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"

# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def tools_for_anthropic(mcp_tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]


def tools_for_openai(mcp_tools: list[dict]) -> list[dict]:
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


def anthropic_turn(client, model: str, system_prompt: str | None, messages: list, tools: list) -> dict:
    """One assistant turn. Returns {tool_calls, assistant_message, tool_result_builder,
    stop_reason, tokens}."""
    kwargs = {"model": model, "max_tokens": 1024, "tools": tools, "messages": messages}
    if system_prompt:
        kwargs["system"] = system_prompt
    response = client.messages.create(**kwargs)

    tool_calls = [
        {"id": block.id, "name": block.name, "input": block.input}
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
        "tokens": {
            "input": response.usage.input_tokens,
            "cached_input": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            "output": response.usage.output_tokens,
        },
    }


# Which output-cap parameter a model accepts, discovered once per model.
# gpt-4o and gpt-4.1 take either; every GPT-5 and o-series model rejects the old
# `max_tokens` outright. Probing rather than hardcoding keeps third-party
# OpenAI-compatible endpoints (the `github` provider) working — Mistral there
# only knows `max_tokens`.
_OUTPUT_CAP_PARAM: dict[str, str] = {}


def openai_turn(client, model: str, system_prompt: str | None, messages: list, tools: list) -> dict:
    """One assistant turn (system prompt must already be in messages)."""
    kwargs = {"model": model, "tools": tools, "tool_choice": "auto", "messages": messages}
    param = _OUTPUT_CAP_PARAM.get(model, "max_completion_tokens")
    try:
        response = client.chat.completions.create(**kwargs, **{param: 1024})
    except Exception as exc:  # noqa: BLE001 — retried below, re-raised if it isn't the cap param
        other = "max_tokens" if param == "max_completion_tokens" else "max_completion_tokens"
        if model in _OUTPUT_CAP_PARAM or param not in str(exc):
            raise
        response = client.chat.completions.create(**kwargs, **{other: 1024})
        param = other
    _OUTPUT_CAP_PARAM[model] = param
    msg = response.choices[0].message

    tool_calls = []
    for call in msg.tool_calls or []:
        try:
            call_input = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            call_input = {}
        tool_calls.append({"id": call.id, "name": call.function.name, "input": call_input})

    assistant_message = {"role": "assistant", "content": msg.content}
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
    details = getattr(response.usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    return {
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
        "tool_result_builder": lambda results: [
            {"role": "tool", "tool_call_id": call_id, "content": text} for call_id, text in results
        ],
        "stop_reason": response.choices[0].finish_reason,
        "tokens": {
            "input": max(response.usage.prompt_tokens - cached, 0),
            "cached_input": cached,
            "output": response.usage.completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Discovery mode — agentic loop from the default surface
# ---------------------------------------------------------------------------


def _surface_tokens(tools: list[dict]) -> int:
    """Rough token cost of the advertised tool list.

    Chars/4, deliberately: an exact count needs a tokenizer per provider and
    would change nothing about the comparison this feeds — surface at turn one
    against surface at its peak, in the same units, for the same run.
    """
    return len(json.dumps(tools)) // 4


def _search_rows(result_text: str) -> tuple[list[dict], int | None]:
    """Ranked rows from shopware-tool-search, plus the candidate pool size.

    Each row is {tool, score, matchedIn, rank}, `rank` being the 1-indexed
    position the server returned it in. The rank is the point: a boolean "was
    the right tool in the results" cannot tell first place from ninth, and the
    difference decides whether a model scrolling a 20-result list ever reaches
    it. The server already computes and sends `score`/`matchedIn`; this used to
    drop both on the floor.
    """
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError, TypeError:
        return [], None
    rows = []
    for position, r in enumerate(payload.get("data", []), start=1):
        tool = r.get("tool") if isinstance(r, dict) else None
        if isinstance(tool, dict) and tool.get("name"):
            rows.append({"tool": tool, "score": r.get("score"), "matched_in": r.get("matchedIn"), "rank": position})
    meta = payload.get("_meta") or {}
    total = meta.get("totalCandidates") if isinstance(meta, dict) else None
    return rows, total


def _search_result_tools(result_text: str) -> list[dict]:
    """Tool definitions returned inline by shopware-tool-search, in MCP tool
    shape ({name, description, inputSchema}) — for making them callable."""
    rows, _ = _search_rows(result_text)
    return [r["tool"] for r in rows]


def _search_contains_expected(result_text: str, expected_tool: str) -> bool:
    return any(t.get("name") == expected_tool for t in _search_result_tools(result_text))


def run_fixture_discovery(
    provider: str,
    client,
    fixture: dict,
    model: str,
    system_prompt: str | None,
    max_steps: int,
    endpoint=ADMIN,
    arm: str = "discovery",
) -> dict:
    prompt = fixture["prompt"]
    # Absent on a negative fixture, where no tool is the right answer. The
    # terminal set is then just `acceptable` (normally empty), which is correct:
    # every non-meta call is already terminal, so any tool the model reaches for
    # is recorded and graded as the over-trigger it is.
    expected_tool = fixture.get("expected_tool")
    acceptable = set(fixture.get("acceptable_tools", []))
    terminal_tools = ({expected_tool} | acceptable) if expected_tool else acceptable
    tools_fn = tools_for_anthropic if provider == "anthropic" else tools_for_openai
    turn_fn = anthropic_turn if provider == "anthropic" else openai_turn

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
    if arm == "isolated" and fixture.get("expected_toolset"):
        enable_toolset(session_id, fixture["expected_toolset"], endpoint=endpoint)
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
    tools = tools_fn(list(catalog.values()))
    # What the advertised surface costs to put in front of the model. The
    # opening figure is the price of v2's promise — a fresh session shows three
    # meta-tools, not the catalogue — and the peak is what the model actually
    # paid on later turns once discovery had pulled tools in. The gap between
    # them is the discovery layer's real context bill.
    surface_tokens = _surface_tokens(tools)
    surface_tokens_peak = surface_tokens

    messages = []
    if provider == "openai" and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    selected_tool, selected_input = None, {}
    fail_reason = None
    # Every non-meta call the model made, in order. The first is `selected_tool`
    # (the old metric); the rest are what recovery looks like.
    attempted_tools = []
    first_tool_correct = None
    resolved = False
    steps_to_correct = None
    dry_run_forced = False
    execution = None
    stop = False
    meta_calls = []
    search_hit = None
    search_rank = None
    search_score = None
    search_candidates = None
    enabled_toolsets = []
    tokens = {"input": 0, "cached_input": 0, "output": 0}
    # Bytes of tool-result payload the model was made to read. A tool that
    # answers correctly but returns 40k of JSON is expensive for every client,
    # and nothing else in the suite would notice.
    payload_bytes = 0
    steps = 0
    t0 = time.time()

    while steps < max_steps:
        steps += 1
        turn = turn_fn(client, model, system_prompt if provider == "anthropic" else None, messages, tools)
        for bucket, count in turn["tokens"].items():
            tokens[bucket] = tokens.get(bucket, 0) + count

        if not turn["tool_calls"]:
            fail_reason = "no_tool_call"
            break

        messages.append(turn["assistant_message"])

        tool_results = []
        catalog_changed = False
        for call in turn["tool_calls"]:
            # Any non-meta call is an answer. Meta navigation tools that are NOT
            # the expected answer (e.g. shopware-toolsets-list on the way to
            # toolset-enable) fall through and are executed as discovery flow —
            # listing toolsets before enabling one is correct.
            answering = call["name"] in terminal_tools or call["name"] not in META_TOOLS
            if answering:
                correct = call["name"] in terminal_tools
                if selected_tool is None:
                    # The first answer is what the old first-try metric measured;
                    # keep it under the same name so historical reports and the
                    # per-tool scorecard still mean what they meant.
                    selected_tool, selected_input = call["name"], call["input"]
                    first_tool_correct = correct

                attempt = {"tool": call["name"], "correct": correct, "step": steps}
                args, forced = prepare_call(call["name"], call["input"])
                dry_run_forced = dry_run_forced or forced

                if not is_executable(call["name"]):
                    # Nothing safe to do with it — no dryRun to hide behind, or
                    # a tool the snapshot has never seen. Graded on selection
                    # alone, which is where the whole suite used to be.
                    attempt["executed"] = False
                    attempted_tools.append(attempt)
                    execution = "skipped_unsafe" if classify(call["name"]) else "skipped_unclassified"
                    if not correct:
                        fail_reason = "wrong_tool"
                    resolved = correct
                    stop = True
                    break

                resp = mcp_call(session_id, call["name"], args, endpoint=endpoint)
                err = mcp_call_error(resp)
                result_text = mcp_result_text(resp) or ""
                payload_bytes += len(result_text.encode("utf-8"))
                execution = "executed"

                ok, reason = check(fixture.get("expect_result"), result_text, err)
                attempt |= {"executed": True, "ok": ok, "reason": reason}
                attempted_tools.append(attempt)

                if correct and ok:
                    resolved = True
                    steps_to_correct = steps
                    stop = True
                    break

                # Wrong tool, or the right tool called badly. Hand back what the
                # server actually said and let the model correct itself — that
                # recovery is the thing being measured, so no hint is injected.
                fail_reason = reason if correct else "wrong_tool"
                tool_results.append((call["id"], result_text or f"Error: {err}"))
                continue

            # Execute discovery meta-tools for real and feed results back.
            resp = mcp_call(session_id, call["name"], call["input"], endpoint=endpoint)
            err = mcp_call_error(resp)
            result_text = mcp_result_text(resp) or (f"Error: {err}" if err else "")
            payload_bytes += len(result_text.encode("utf-8"))
            tool_results.append((call["id"], result_text))
            meta_calls.append(
                {
                    "tool": call["name"],
                    "input": call["input"],
                    "result_preview": result_text[:300],
                }
            )
            if call["name"] == "shopware-tool-search":
                rows, candidates = _search_rows(result_text)
                hit = any(r["tool"].get("name") == expected_tool for r in rows)
                search_hit = hit if search_hit is None else (search_hit or hit)
                if candidates is not None:
                    search_candidates = candidates
                # A fixture may search several times with different wording.
                # Keep the best placement the expected tool ever reached: that
                # is the ranking the model had its best chance from, so a later
                # vaguer query cannot make the catalogue look worse than it is.
                for r in rows:
                    if r["tool"].get("name") == expected_tool and (search_rank is None or r["rank"] < search_rank):
                        search_rank, search_score = r["rank"], r["score"]
                # Make search-surfaced tools callable next turn.
                for t in (r["tool"] for r in rows):
                    if t["name"] not in catalog:
                        catalog[t["name"]] = t
                        catalog_changed = True
            if call["name"] == "shopware-toolset-enable" and not err:
                enabled_toolsets.append(call["input"].get("toolset", ""))
                # Simulate tools/list_changed: re-fetch the advertised surface.
                for t in mcp_tools_list_all(session_id, endpoint=endpoint):
                    if t["name"] not in catalog:
                        catalog[t["name"]] = t
                        catalog_changed = True

        # `stop` rather than "a tool was selected": a wrong first pick no longer
        # ends the run, because whether the model recovers from it is the point.
        if stop:
            break

        if catalog_changed:
            tools = tools_fn(list(catalog.values()))
            surface_tokens_peak = max(surface_tokens_peak, _surface_tokens(tools))

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
        if not attempted_tools:
            fail_reason = "step_cap"

    latency = round(time.time() - t0, 2)

    meta_names = {m["tool"] for m in meta_calls}
    used_search = "shopware-tool-search" in meta_names
    used_toolsets = bool(meta_names & {"shopware-toolsets-list", "shopware-toolset-enable"})
    if selected_tool is None:
        discovery_path = "none"
    elif used_search and used_toolsets:
        discovery_path = "mixed"
    elif used_search:
        discovery_path = "search"
    elif used_toolsets:
        discovery_path = "toolsets"
    else:
        discovery_path = "direct"

    if is_negative(fixture) or execution is None:
        # Nothing was executed, so the old rule is the only one available: a
        # negative fixture passes by declining, and a fixture whose model never
        # answered has nothing to assert on.
        passed = is_correct(selected_tool, fixture, fail_reason)
    else:
        # The call has to have run and satisfied the fixture's expectation, not
        # merely been named. `resolved` covers recovery: the model may have got
        # there on a later attempt.
        passed = resolved
    if not passed and fail_reason is None:
        fail_reason = "wrong_tool"
    if passed and fail_reason == "no_tool_call":
        # Declining IS the pass on a negative fixture. Leaving the reason set
        # would render a passing fixture with a failure reason attached.
        fail_reason = None

    expected_toolset = fixture.get("expected_toolset")
    enabled_correct_toolset = None
    if expected_toolset and enabled_toolsets:
        enabled_correct_toolset = expected_toolset in enabled_toolsets

    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": arm,
        "prompt": prompt,
        "expected_tool": expected_tool,
        "expected_toolset": expected_toolset,
        "selected_tool": selected_tool,
        "selected_input": selected_input,
        "passed": passed,
        "fail_reason": None if passed else fail_reason,
        # Whether the FIRST answer was the right tool. The scorecard reads this
        # for precision rather than `passed`, which since recovery no longer
        # implies the first pick was correct — a recovered fixture would
        # otherwise credit the wrong tool with a good selection.
        "first_tool_correct": first_tool_correct,
        # Both halves: the first answer named the right tool AND that call
        # worked. `ok` alone is only "the server accepted it", which a wrong
        # tool called competently also satisfies.
        "first_try": bool(attempted_tools)
        and attempted_tools[0]["correct"]
        and attempted_tools[0].get("ok", True) is True,
        "recovered": passed and len(attempted_tools) > 1,
        "attempted_tools": attempted_tools,
        "wrong_calls": sum(1 for a in attempted_tools if not a["correct"]),
        "steps_to_correct": steps_to_correct,
        "execution": execution,
        "dry_run_forced": dry_run_forced,
        "steps": steps,
        "meta_calls": meta_calls,
        "discovery_path": discovery_path,
        "search_hit": search_hit,
        "search_rank": search_rank,
        "search_score": search_score,
        "search_candidates": search_candidates,
        "enabled_toolsets": enabled_toolsets,
        "enabled_correct_toolset": enabled_correct_toolset,
        "latency_s": latency,
        "tokens": tokens,
        "payload_bytes": payload_bytes,
        "surface_tokens": surface_tokens,
        "surface_tokens_peak": surface_tokens_peak,
        "notes": fixture.get("notes", ""),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "anthropic": "claude-sonnet-4-6",
    # This is what CI resolves the PRIMARY eval to — the workflow's `eval_model`
    # input defaults to empty, so changing this constant changes the gating
    # model. It used to be gpt-4o, which made the primary and the gpt-4o-mini
    # second validator two variants of one model: same vendor, same generation,
    # same function-calling stack, so they tended to fail for the same reasons
    # and the both-fail bucket carried little independent signal.
    #
    # gpt-5.4-mini is a generation removed from gpt-4o-mini while being cheaper
    # than gpt-4o ($0.75 vs ~$2.50 per 1M input) at the same latency. Measured
    # on the 24 disambiguation fixtures — the category most sensitive to
    # description quality — it scored 19/19 against gpt-4o's 18/19.
    "openai": "gpt-5.4-mini",
    # A non-OpenAI publisher on purpose: as the second validator its value is
    # being an independent implementation, so it catches tool-description
    # problems that are specific to one vendor's function-calling behaviour.
    "github": "mistral-ai/mistral-medium-2505",
}


def write_summary_row(provider, model, discovery, rate, ok, args) -> dict:
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
    row = {
        "suite": args.suite_label or args.endpoint,
        "provider": provider,
        "model": model,
        "rate": rate,
        "graded": len(graded or []),
        "errored": sum(1 for r in graded or [] if r.get("error")),
        "throttled": throttled,
        "gate": "PASS" if ok else "FAIL",
        "advisory": bool(args.advisory),
        # Per-owning-repo split, so the summary can say whether a failure landed
        # in core or in an optional plugin. One aggregate rate cannot. Built
        # over executed() — the same exclusions as the overall rate, so the
        # per-tier numbers stay comparable with it.
        "by_tier": breakdown(executed(graded)),
        # The row is what the consolidated summary renders from, so the cost has
        # to travel with it — otherwise the summary would have to re-read every
        # full report just to add one column.
        "cost": run_cost(graded, model, load_pricing()),
    }

    # Also on stdout: the job summary now only appears once every eval has run,
    # so a timed-out or cancelled job would otherwise show nothing at all.
    print(
        f"\nSummary row: {row['suite']} | {provider} {model} | {round(rate * 100)}% | "
        f"graded={row['graded']} errors={row['errored']} throttled={throttled} | {row['gate']}"
    )

    if args.summary_row:
        path = Path(args.summary_row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, indent=2))

    if throttled:
        print(f"\n::warning::{throttled} fixture(s) hit provider rate limits — results are understated.")
    return row


def skipped_result(fixture: dict, mode: str) -> dict:
    """A fixture whose expected tool is not registered on this instance is
    skipped, not failed — e.g. a dev-tools fixture on an instance without the
    SwagMcpDevTools bundle. Skipped fixtures are excluded from scoring."""
    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": mode,
        "prompt": fixture["prompt"],
        "expected_tool": fixture.get("expected_tool"),
        "selected_tool": None,
        "passed": False,
        "skipped": True,
        "skip_reason": "expected tool not registered on this instance",
    }


def error_result(fixture: dict, mode: str, exc: Exception) -> dict:
    """Uniform failure record for a fixture that raised."""
    record = {
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
        record |= {
            "steps": 0,
            "meta_calls": [],
            "discovery_path": "none",
            "search_hit": None,
            "search_rank": None,
            "search_score": None,
            "search_candidates": None,
            "enabled_correct_toolset": None,
        }
    return record


def run_fixtures_concurrently(fixtures: list[dict], worker, workers: int) -> list[dict]:
    """Run `worker(fixture)` over the fixtures with a bounded thread pool.

    Fixtures are independent (each discovery run opens its own MCP session), so
    they parallelize cleanly — the wall-clock win is large because almost all of
    the time is spent waiting on the LLM API. Results keep fixture order for the
    report; progress is printed as each one lands, which is why every line is
    prefixed with the fixture id.
    """
    results: list[dict | None] = [None] * len(fixtures)
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
            print(f"  {DIM}[{completed:02d}/{len(fixtures):02d}]{RESET} {result['_line']}")
    return [r for r in results if r is not None]


def run_discovery_pass(
    provider,
    client,
    fixtures,
    model,
    system_prompt,
    default_max_steps,
    available_tools,
    endpoint=ADMIN,
    workers=1,
):
    print(f"\n{BOLD}── Mode: discovery (default surface + agentic loop) ──{RESET}\n")
    print(f"  concurrency={workers}\n")

    def worker(fixture: dict) -> dict:
        # A negative fixture names no tool, so there is nothing to be missing —
        # it always runs. (It is easier on an instance with fewer plugins, since
        # fewer tools exist to be wrongly picked; that is a caveat on comparing
        # negative rates across instances, not a reason to skip.)
        expected = fixture.get("expected_tool")
        if expected and expected not in available_tools:
            result = skipped_result(fixture, "discovery")
            result["_line"] = _render(result)
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
        result["_line"] = _render(result)
        return result

    return run_fixtures_concurrently(fixtures, worker, workers)


# Categories that only make sense against the live discovery surface. `meta`
# fixtures expect a meta-tool, which the diagnostic arms withhold; `discovery`
# fixtures exist to exercise search and enablement, which the arms bypass; a
# `negative` fixture asks whether anything bites, and pre-enabling a group to
# ask that would be a different question.
ARM_SKIP_CATEGORIES = frozenset({"meta", "discovery", "negative"})


def triage_arms(
    provider,
    client,
    discovery_results,
    fixtures,
    model,
    system_prompt,
    default_max_steps,
    endpoint=ADMIN,
    workers=1,
    arms=("isolated", "full"),
) -> dict[str, list[dict]]:
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

    out = {}
    for arm in arms:
        print(f"\n{BOLD}── Triage arm: {arm} ({len(failed)} discovery failures) ──{RESET}\n")

        def worker(fixture, arm=arm):
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
            result["_line"] = _render(result)
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
        choices=["anthropic", "openai", "github"],
        default=os.environ.get("EVAL_PROVIDER", "anthropic"),
        help="anthropic | openai | github (GitHub Models: free, OpenAI-compatible, auth via GITHUB_TOKEN)",
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
    parser.add_argument(
        "--no-system-prompt", action="store_true", help="Skip the MCP server system prompt (ad-hoc debugging)"
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


def resolve_model(provider: str, requested: str | None) -> str:
    """CLI flag wins, then EVAL_MODEL, then the provider default.

    PROVIDER_DEFAULTS is what CI resolves the gating model to, so changing that
    constant changes which model gates.
    """
    return requested or os.environ.get("EVAL_MODEL") or PROVIDER_DEFAULTS[provider]


def require_credentials(provider: str, endpoint_name: str) -> tuple[str, str]:
    """Check the server and provider credentials this run needs.

    Returns the (name, value) of the provider credential, because build_client
    needs the value and the `github` provider's differs from OpenAI's.
    """
    required = [("SW_BASE_URL", SW_BASE_URL)]
    if endpoint_name == "store":
        required.append(("SW_SC_ACCESS_KEY", SW_SC_ACCESS_KEY))
    else:
        required += [("SW_ACCESS_KEY", SW_ACCESS_KEY), ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)]
    credential = {
        "anthropic": ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        "openai": ("OPENAI_API_KEY", OPENAI_API_KEY),
        "github": ("GITHUB_TOKEN", GITHUB_TOKEN),
    }[provider]
    required.append(credential)
    missing = [var for var, val in required if not val]
    if missing:
        raise ConfigError(f"{', '.join(missing)} is not set.")
    return credential


def build_client(provider: str, credential: tuple[str, str]):
    """The provider SDK client. Imported lazily so a run with one provider does
    not require the other's package to be installed."""
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=credential[1])
    from openai import OpenAI

    # GitHub Models speaks the OpenAI wire format, so the same adapter and turn
    # function work — only the base URL and credential differ.
    return OpenAI(
        api_key=credential[1],
        base_url=GITHUB_MODELS_BASE_URL if provider == "github" else None,
    )


def fixtures_path_for(endpoint_name: str, override: str | None) -> Path:
    if override:
        return Path(override)
    return Path(__file__).parent / ("fixtures_store.yaml" if endpoint_name == "store" else "fixtures.yaml")


def load_fixtures(path: Path, category: str | None = None, fixture_id: str | None = None) -> list[dict]:
    fixtures = yaml.safe_load(path.read_text())["fixtures"]
    if category:
        fixtures = [f for f in fixtures if f.get("category") == category]
    if fixture_id:
        fixtures = [f for f in fixtures if f["id"] == fixture_id]
    if not fixtures:
        raise ConfigError("No fixtures matched the filter.")
    return fixtures


def fetch_system_prompt(endpoint, enabled: bool = True) -> str | None:
    """The server's own instructions plus its context prompts, as the model sees
    them. Disabled by --no-system-prompt for ad-hoc debugging."""
    if not enabled:
        print("System prompt: disabled (--no-system-prompt)")
        return None
    session_id, server_instructions = mcp_init(endpoint=endpoint)
    prompt = mcp_fetch_system_prompt(session_id, server_instructions, endpoint=endpoint)
    sections = [line for line in prompt.split("\n") if line.startswith("# ")]
    print(f"System prompt: {len(sections)} sections, {len(prompt)} chars")
    return prompt


def probe_catalogue(endpoint) -> set[str]:
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
    fixtures: list[dict],
    discovery: list[dict] | None,
    system_prompt_enabled: bool,
    max_steps: int,
    arm_results: dict[str, list[dict]] | None = None,
) -> dict:
    """The JSON report. Pure: no writing, so its shape can be asserted directly."""
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "server": SW_BASE_URL,
        "provider": provider,
        "model": model,
        "modes": {},
        "fixtures": len(fixtures),
        "system_prompt": system_prompt_enabled,
        "max_steps": max_steps,
    }
    if discovery is not None:
        report["modes"]["discovery"] = {
            "passed": sum(1 for r in scored(discovery) if r["passed"]),
            "failed": sum(1 for r in scored(discovery) if not r["passed"]),
            "skipped": sum(1 for r in discovery if r.get("skipped")),
            "results": discovery,
        }
        report["discovery_summary"] = discovery_summary(discovery)
    # Diagnostic arms sit alongside discovery under the same key, so
    # compare_runs and the gate — which both read modes["discovery"] by name —
    # are untouched by their presence.
    for arm, records in (arm_results or {}).items():
        report["modes"][arm] = {
            "passed": sum(1 for r in scored(records) if r["passed"]),
            "failed": sum(1 for r in scored(records) if not r["passed"]),
            "skipped": sum(1 for r in records if r.get("skipped")),
            "results": records,
        }
    # Per-owning-repo rates, so the report answers "which codebase regressed"
    # without re-deriving attribution downstream. `or []` covers discovery not
    # having run — parse_modes rejects an empty mode list, so that is unreachable
    # via the CLI, but an empty table is the honest answer rather than a crash for
    # a direct caller.
    report["by_tier"] = breakdown(executed(discovery or []))
    # What this run cost, in dollars and in the volume behind them. Recorded in
    # the report rather than only printed so cost_drift.py can compare a run
    # against its predecessor without re-deriving anything.
    report["cost"] = run_cost(discovery or [], model, load_pricing())
    return report


def print_gate(verdict: dict, args) -> None:
    """The gate block. Reads only the verdict dict, so gate_verdict stays the
    single place the pass/fail decision is made."""
    gating, graded = verdict["gating"], verdict["graded"]
    print(
        f"\nGate: {verdict['passed']}/{len(gating)} = {round(verdict['rate'] * 100)}% "
        f"(threshold {round(args.min_pass_rate * 100)}%) → {'PASS' if verdict['quality_ok'] else 'FAIL'}"
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
            f"({round(verdict['error_rate'] * 100)}%, budget {round(args.max_error_rate * 100)}%) → "
            f"{'within budget' if verdict['run_valid'] else 'RUN INVALID'}"
        )
    if not verdict["quality_ok"]:
        print(f"  below threshold; failing: {', '.join(r['id'] for r in gating if not r['passed'])}")
    if not verdict["core_ok"]:
        core_failed = [r["id"] for r in gating if not r["passed"] and owner_of(r.get("expected_tool", "")) == CORE]
        print(f"  {RED}core below threshold{RESET}; failing: {', '.join(core_failed)}")
    if not verdict["run_valid"]:
        print("  too many fixtures errored to trust this run — fix the server/provider, then re-run.")


def run_suite(args) -> int:
    """One eval run end to end. Returns the process exit code."""
    provider = args.provider
    model = resolve_model(provider, args.model)
    modes = parse_modes(args.modes)
    endpoint = endpoint_by_name(args.endpoint)
    credential = require_credentials(provider, args.endpoint)
    client = build_client(provider, credential)
    fixtures = load_fixtures(fixtures_path_for(args.endpoint, args.fixtures), args.category, args.id)

    print(f"{BOLD}Shopware MCP LLM Eval (v2 discovery){RESET}")
    print(f"Server:   {SW_BASE_URL}  ({endpoint.name} endpoint)")
    print(f"Provider: {provider}")
    print(f"Model:    {model}")
    print(f"Modes:    {', '.join(modes)}")
    print(f"Fixtures: {len(fixtures)}")

    print("\nInitializing MCP session for system prompt...")
    system_prompt = fetch_system_prompt(endpoint, enabled=not args.no_system_prompt)

    available_tools = probe_catalogue(endpoint)
    # `.get()`: a negative fixture names no tool, so there is nothing that could
    # be absent from the catalogue — and indexing it here crashed the whole run
    # before a single fixture had been graded.
    absent = sorted({f["expected_tool"] for f in fixtures if f.get("expected_tool")} - available_tools)
    if absent:
        print(f"Catalogue: {len(available_tools)} tools; will skip fixtures for absent: {', '.join(absent)}")

    results_discovery = None
    if "discovery" in modes:
        results_discovery = run_discovery_pass(
            provider,
            client,
            fixtures,
            model,
            system_prompt,
            args.max_steps,
            available_tools,
            endpoint=endpoint,
            workers=args.discovery_concurrency,
        )

    # `_line` is progress-display scaffolding, not part of the report contract.
    for result in results_discovery or []:
        result.pop("_line", None)

    if results_discovery:
        print_single_mode(results_discovery, "discovery")
        print_discovery_block(results_discovery)

    arm_results = {}
    if args.triage:
        arm_results = triage_arms(
            provider,
            client,
            results_discovery,
            fixtures,
            model,
            system_prompt,
            args.max_steps,
            endpoint=endpoint,
            workers=args.discovery_concurrency,
        )
        for records in arm_results.values():
            for record in records:
                record.pop("_line", None)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_path = Path(args.output or BASE / "results" / f"eval-{provider}-{ts}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_report(
                provider,
                model,
                fixtures,
                results_discovery,
                not args.no_system_prompt,
                args.max_steps,
                arm_results,
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
        results_discovery,
        min_pass_rate=args.min_pass_rate,
        min_core_pass_rate=args.min_core_pass_rate,
        max_error_rate=args.max_error_rate,
    )
    print_gate(verdict, args)
    write_summary_row(provider, model, results_discovery, verdict["rate"], verdict["ok"], args)
    return 0 if verdict["ok"] else 1


# Exit codes, because the workflow treats them differently. 1 means the run
# happened and the gate said no, which is what the advisory windows (REBASELINE,
# catalogue drift) are allowed to downgrade to a warning. CRASH_EXIT means the
# run did not produce a verdict at all, and no advisory window may swallow that:
# a green job that actually crashed is worse than a red one.
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
