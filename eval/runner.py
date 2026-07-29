#!/usr/bin/env python3
"""
Shopware MCP LLM Eval Runner (MCP Server v2: dynamic tool discovery)

Runs each fixture in up to two modes per provider:

  baseline   All toolsets enabled, the full catalogue passed flat to the LLM
             in a single request. Grades the FIRST tool call. This is the v1
             behaviour and the comparison reference.

  discovery  Only the default advertised surface is passed. The runner
             executes discovery meta-tool calls (shopware-tool-search,
             shopware-toolsets-list, shopware-toolset-enable) for real
             against the server and feeds results back in an agentic loop.
             The first NON-meta tool call is terminal and graded against
             expected_tool. Meta steps are free but counted.

The comparison answers: does dynamic discovery make it harder for the model
to find the right tool, which discovery path does it take, and what does
discovery cost in tokens and steps?

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import yaml

from eval.report import (
    BOLD,
    DIM,
    RED,
    RESET,
    _render,
    print_comparison,
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
    scored,
    total_tokens,
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
    endpoint_by_name,
    mcp_call,
    mcp_call_error,
    mcp_fetch_system_prompt,
    mcp_init,
    mcp_result_text,
    mcp_tools_list_all,
)
from ownership import CORE, breakdown, owner_of

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
        "tokens": {
            "input": response.usage.input_tokens,
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
    return {
        "tool_calls": tool_calls,
        "assistant_message": assistant_message,
        "tool_result_builder": lambda results: [
            {"role": "tool", "tool_call_id": call_id, "content": text} for call_id, text in results
        ],
        "stop_reason": response.choices[0].finish_reason,
        "tokens": {
            "input": response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Baseline mode — single shot against the full catalogue (v1 behaviour)
# ---------------------------------------------------------------------------


def run_fixture_baseline(
    provider: str, client, tools: list[dict], fixture: dict, model: str, system_prompt: str | None
) -> dict:
    prompt = fixture["prompt"]
    messages = []
    if provider == "openai" and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    turn_fn = anthropic_turn if provider == "anthropic" else openai_turn
    t0 = time.time()
    turn = turn_fn(client, model, system_prompt if provider == "anthropic" else None, messages, tools)
    latency = round(time.time() - t0, 2)

    selected_tool, selected_input = None, {}
    if turn["tool_calls"]:
        selected_tool = turn["tool_calls"][0]["name"]
        selected_input = turn["tool_calls"][0]["input"]

    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": "baseline",
        "prompt": prompt,
        "expected_tool": fixture["expected_tool"],
        "selected_tool": selected_tool,
        "selected_input": selected_input,
        "passed": is_correct(selected_tool, fixture),
        "steps": 1,
        "latency_s": latency,
        "stop_reason": turn["stop_reason"],
        "tokens": turn["tokens"],
        "notes": fixture.get("notes", ""),
    }


# ---------------------------------------------------------------------------
# Discovery mode — agentic loop from the default surface
# ---------------------------------------------------------------------------


def _search_result_tools(result_text: str) -> list[dict]:
    """Extract the tool definitions returned inline by shopware-tool-search,
    in MCP tool shape ({name, description, inputSchema})."""
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError, TypeError:
        return []
    tools = []
    for r in payload.get("data", []):
        tool = r.get("tool") if isinstance(r, dict) else None
        if isinstance(tool, dict) and tool.get("name"):
            tools.append(tool)
    return tools


def _search_contains_expected(result_text: str, expected_tool: str) -> bool:
    return any(t.get("name") == expected_tool for t in _search_result_tools(result_text))


def run_fixture_discovery(
    provider: str, client, fixture: dict, model: str, system_prompt: str | None, max_steps: int, endpoint=ADMIN
) -> dict:
    prompt = fixture["prompt"]
    expected_tool = fixture["expected_tool"]
    acceptable = set(fixture.get("acceptable_tools", []))
    terminal_tools = {expected_tool} | acceptable
    tools_fn = tools_for_anthropic if provider == "anthropic" else tools_for_openai
    turn_fn = anthropic_turn if provider == "anthropic" else openai_turn

    # Fresh session per fixture: toolset enablement persists per Mcp-Session-Id
    # and would leak across fixtures on a shared session.
    session_id, _ = mcp_init(endpoint=endpoint)

    # Callable-tool catalogue by name. Starts as the advertised default surface.
    # Grows when a toolset is enabled (re-fetched tools/list) OR when
    # shopware-tool-search returns a tool inline — a search-surfaced tool is
    # directly callable because the allowlist, not advertising, is the call
    # boundary. This mirrors how a real MCP client exposes discovered tools.
    catalog = {t["name"]: t for t in mcp_tools_list_all(session_id, endpoint=endpoint)}
    tools = tools_fn(list(catalog.values()))

    messages = []
    if provider == "openai" and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    selected_tool, selected_input = None, {}
    fail_reason = None
    meta_calls = []
    search_hit = None
    enabled_toolsets = []
    tokens = {"input": 0, "output": 0}
    steps = 0
    t0 = time.time()

    while steps < max_steps:
        steps += 1
        turn = turn_fn(client, model, system_prompt if provider == "anthropic" else None, messages, tools)
        tokens["input"] += turn["tokens"]["input"]
        tokens["output"] += turn["tokens"]["output"]

        if not turn["tool_calls"]:
            fail_reason = "no_tool_call"
            break

        messages.append(turn["assistant_message"])

        tool_results = []
        catalog_changed = False
        for call in turn["tool_calls"]:
            # A call is terminal (graded) when it is the expected/acceptable
            # tool, or any non-meta tool. Meta navigation tools that are NOT the
            # expected answer (e.g. shopware-toolsets-list on the way to
            # toolset-enable) are executed and fed back so the model can proceed
            # — listing toolsets before enabling one is correct discovery flow.
            terminal = call["name"] in terminal_tools or call["name"] not in META_TOOLS
            if terminal:
                # Grade the selection; do NOT execute (no-mutation policy).
                selected_tool = call["name"]
                selected_input = call["input"]
                break

            # Execute discovery meta-tools for real and feed results back.
            resp = mcp_call(session_id, call["name"], call["input"], endpoint=endpoint)
            err = mcp_call_error(resp)
            result_text = mcp_result_text(resp) or (f"Error: {err}" if err else "")
            tool_results.append((call["id"], result_text))
            meta_calls.append(
                {
                    "tool": call["name"],
                    "input": call["input"],
                    "result_preview": result_text[:300],
                }
            )
            if call["name"] == "shopware-tool-search":
                found = _search_result_tools(result_text)
                hit = any(t.get("name") == expected_tool for t in found)
                search_hit = hit if search_hit is None else (search_hit or hit)
                # Make search-surfaced tools callable next turn.
                for t in found:
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

        if selected_tool is not None:
            break

        if catalog_changed:
            tools = tools_fn(list(catalog.values()))

        builder_output = turn["tool_result_builder"](tool_results)
        if isinstance(builder_output, list):
            messages.extend(builder_output)
        else:
            messages.append(builder_output)
    else:
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

    passed = is_correct(selected_tool, fixture)
    if not passed and fail_reason is None:
        fail_reason = "wrong_tool"

    expected_toolset = fixture.get("expected_toolset")
    enabled_correct_toolset = None
    if expected_toolset and enabled_toolsets:
        enabled_correct_toolset = expected_toolset in enabled_toolsets

    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "mode": "discovery",
        "prompt": prompt,
        "expected_tool": expected_tool,
        "expected_toolset": expected_toolset,
        "selected_tool": selected_tool,
        "selected_input": selected_input,
        "passed": passed,
        "fail_reason": None if passed else fail_reason,
        "steps": steps,
        "meta_calls": meta_calls,
        "discovery_path": discovery_path,
        "search_hit": search_hit,
        "enabled_toolsets": enabled_toolsets,
        "enabled_correct_toolset": enabled_correct_toolset,
        "latency_s": latency,
        "tokens": tokens,
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


def write_summary_row(provider, model, baseline, discovery, rate, ok, args) -> dict:
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
    graded = discovery if discovery is not None else baseline
    throttled = count_rate_limited(baseline) + count_rate_limited(discovery)
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
        "expected_tool": fixture["expected_tool"],
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
        "expected_tool": fixture["expected_tool"],
        "selected_tool": None,
        "passed": False,
        "error": str(exc),
    }
    if mode == "discovery":
        record |= {
            "steps": 0,
            "meta_calls": [],
            "discovery_path": "none",
            "search_hit": None,
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


def run_baseline_pass(provider, client, fixtures, model, system_prompt, available_tools, endpoint=ADMIN, workers=1):
    print(f"\n{BOLD}── Mode: baseline (full catalogue, single shot) ──{RESET}\n")
    session_id, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(session_id, endpoint=endpoint)
    mcp_tools = mcp_tools_list_all(session_id, endpoint=endpoint)
    tools_fn = tools_for_anthropic if provider == "anthropic" else tools_for_openai
    tools = tools_fn(mcp_tools)
    print(f"  Catalogue: {len(mcp_tools)} tools (all toolsets enabled), concurrency={workers}\n")

    def worker(fixture: dict) -> dict:
        if fixture["expected_tool"] not in available_tools:
            result = skipped_result(fixture, "baseline")
        else:
            try:
                result = run_fixture_baseline(provider, client, tools, fixture, model, system_prompt)
            except Exception as exc:  # noqa: BLE001 — recorded as a failed fixture
                result = error_result(fixture, "baseline", exc)
        result["_line"] = _render(result)
        return result

    return run_fixtures_concurrently(fixtures, worker, workers)


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
        if fixture["expected_tool"] not in available_tools:
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
    parser.add_argument(
        "--modes", default="baseline,discovery", help="Comma-separated: baseline, discovery (default: both)"
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Max assistant turns in discovery mode (per-fixture max_steps overrides)",
    )
    parser.add_argument(
        "--no-system-prompt", action="store_true", help="Skip the MCP server system prompt (ad-hoc debugging)"
    )
    # Fixtures are independent and almost entirely LLM-API-bound, so running them
    # concurrently cuts wall-clock roughly linearly. Discovery is kept lower
    # because each step also hits the MCP endpoint, which throttles (HTTP 429).
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("EVAL_CONCURRENCY", "8")),
        help="Parallel fixtures in baseline mode (default 8; 1 = sequential)",
    )
    parser.add_argument(
        "--discovery-concurrency",
        type=int,
        default=int(os.environ.get("EVAL_DISCOVERY_CONCURRENCY", "4")),
        help="Parallel fixtures in discovery mode (default 4; lower, it also calls MCP)",
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
    unknown = [m for m in modes if m not in ("baseline", "discovery")]
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
    baseline: list[dict] | None,
    discovery: list[dict] | None,
    system_prompt_enabled: bool,
    max_steps: int,
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
    if baseline is not None:
        report["modes"]["baseline"] = {
            "passed": sum(1 for r in scored(baseline) if r["passed"]),
            "failed": sum(1 for r in scored(baseline) if not r["passed"]),
            "skipped": sum(1 for r in baseline if r.get("skipped")),
            "tokens": total_tokens(scored(baseline)),
            "results": baseline,
        }
    if discovery is not None:
        report["modes"]["discovery"] = {
            "passed": sum(1 for r in scored(discovery) if r["passed"]),
            "failed": sum(1 for r in scored(discovery) if not r["passed"]),
            "skipped": sum(1 for r in discovery if r.get("skipped")),
            "results": discovery,
        }
        report["discovery_summary"] = discovery_summary(discovery)
    # Per-owning-repo rates over the gating mode, so the report answers "which
    # codebase regressed" without re-deriving attribution downstream. Discovery
    # is the mode that gates; baseline only carries attribution when it ran
    # alone. `or []` covers neither having run — parse_modes rejects an empty
    # mode list, so that is unreachable via the CLI, but an empty table is the
    # honest answer rather than a crash for a direct caller.
    gating = discovery if discovery is not None else baseline
    report["by_tier"] = breakdown(executed(gating or []))
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
    absent = sorted({f["expected_tool"] for f in fixtures} - available_tools)
    if absent:
        print(f"Catalogue: {len(available_tools)} tools; will skip fixtures for absent: {', '.join(absent)}")

    results_baseline = results_discovery = None
    if "baseline" in modes:
        results_baseline = run_baseline_pass(
            provider,
            client,
            fixtures,
            model,
            system_prompt,
            available_tools,
            endpoint=endpoint,
            workers=args.concurrency,
        )
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
    for bucket in (results_baseline, results_discovery):
        for result in bucket or []:
            result.pop("_line", None)

    if results_baseline and results_discovery:
        print_comparison(results_baseline, results_discovery)
        print_discovery_block(results_discovery, results_baseline)
    elif results_discovery:
        print_single_mode(results_discovery, "discovery")
        print_discovery_block(results_discovery, None)
    elif results_baseline:
        print_single_mode(results_baseline, "baseline")

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_path = Path(args.output or BASE / "results" / f"eval-{provider}-{ts}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_report(
                provider,
                model,
                fixtures,
                results_baseline,
                results_discovery,
                not args.no_system_prompt,
                args.max_steps,
            ),
            indent=2,
        )
    )
    print(f"Report saved: {output_path}")

    # Gate: discovery mode is the v2 target behaviour; baseline is the comparison
    # reference and stays advisory when both run. Skipped fixtures (tool absent on
    # this instance) do not gate.
    #
    # The LLM eval is a quality signal against a nondeterministic model, so it
    # gates on a pass-rate threshold rather than a strict 100% — a couple of
    # borderline fixtures shouldn't flip CI red, but a real regression (the rate
    # collapsing) still fails. Each failed discovery fixture is also retried once
    # (see run_discovery_pass). Set --min-pass-rate 1.0 for strict.
    verdict = gate_verdict(
        results_discovery if results_discovery is not None else results_baseline,
        min_pass_rate=args.min_pass_rate,
        min_core_pass_rate=args.min_core_pass_rate,
        max_error_rate=args.max_error_rate,
    )
    print_gate(verdict, args)
    write_summary_row(provider, model, results_baseline, results_discovery, verdict["rate"], verdict["ok"], args)
    return 0 if verdict["ok"] else 1


def main() -> int:
    try:
        return run_suite(build_parser().parse_args())
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
