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
    python eval/run.py                                  # both modes, Anthropic
    python eval/run.py --provider openai --model gpt-4o
    python eval/run.py --modes discovery --max-steps 8
    python eval/run.py --category disambiguation
    python eval/run.py --id disambig_count_vs_search
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

# mcp_client lives at the repo root (shared by the eval and functional layers).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import (  # noqa: E402
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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

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
            "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": text}
                for call_id, text in results
            ],
        },
        "stop_reason": response.stop_reason,
        "tokens": {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
        },
    }


def openai_turn(client, model: str, system_prompt: str | None, messages: list, tools: list) -> dict:
    """One assistant turn (system prompt must already be in messages)."""
    response = client.chat.completions.create(
        model=model, max_tokens=1024, tools=tools, tool_choice="auto", messages=messages,
    )
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
            {"role": "tool", "tool_call_id": call_id, "content": text}
            for call_id, text in results
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

def is_correct(selected_tool: str | None, fixture: dict) -> bool:
    """A selection is correct if it is the expected tool or any tool listed in
    the fixture's optional `acceptable_tools` (for genuinely multi-valid prompts)."""
    if selected_tool is None:
        return False
    return (selected_tool == fixture["expected_tool"]
            or selected_tool in fixture.get("acceptable_tools", []))


def run_fixture_baseline(provider: str, client, tools: list[dict], fixture: dict,
                         model: str, system_prompt: str | None) -> dict:
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
    except (json.JSONDecodeError, TypeError):
        return []
    tools = []
    for r in payload.get("data", []):
        tool = r.get("tool") if isinstance(r, dict) else None
        if isinstance(tool, dict) and tool.get("name"):
            tools.append(tool)
    return tools


def _search_contains_expected(result_text: str, expected_tool: str) -> bool:
    return any(t.get("name") == expected_tool for t in _search_result_tools(result_text))


def run_fixture_discovery(provider: str, client, fixture: dict, model: str,
                          system_prompt: str | None, max_steps: int, endpoint=ADMIN) -> dict:
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
        turn = turn_fn(client, model, system_prompt if provider == "anthropic" else None,
                       messages, tools)
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
            meta_calls.append({
                "tool": call["name"],
                "input": call["input"],
                "result_preview": result_text[:300],
            })
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

RESET = "\033[0m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[0;36m"


def pct_color(pct: int) -> str:
    return GREEN if pct >= 80 else (YELLOW if pct >= 50 else RED)


def scored(results: list[dict]) -> list[dict]:
    """Results that count toward pass/fail — skipped fixtures are excluded."""
    return [r for r in results if not r.get("skipped")]


def score(results: list[dict]) -> dict:
    """Return per-tool and per-category pass counts (skipped fixtures excluded)."""
    tools: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    for r in scored(results):
        t = r["expected_tool"]
        c = r["category"]
        tools.setdefault(t, {"pass": 0, "total": 0})
        cats.setdefault(c, {"pass": 0, "total": 0})
        tools[t]["total"] += 1
        cats[c]["total"] += 1
        if r["passed"]:
            tools[t]["pass"] += 1
            cats[c]["pass"] += 1
    return {"tools": tools, "cats": cats}


def total_tokens(results: list[dict]) -> dict:
    agg = {"input": 0, "output": 0}
    for r in results:
        t = r.get("tokens") or {}
        agg["input"] += t.get("input", 0)
        agg["output"] += t.get("output", 0)
    return agg


def print_comparison(baseline: list[dict], discovery: list[dict]):
    s_base = score(baseline)
    s_disc = score(discovery)

    total = len(scored(baseline))
    p_base = sum(1 for r in scored(baseline) if r["passed"])
    p_disc = sum(1 for r in scored(discovery) if r["passed"])
    skipped = sum(1 for r in discovery if r.get("skipped"))

    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}Comparison: baseline (full catalogue)  vs  discovery (default surface){RESET}")
    print(f"{'='*78}")
    skip_note = f"  ({skipped} skipped — tool not on this instance)" if skipped else ""
    print(f"  Overall: {GREEN}{p_base}/{total}{RESET} baseline  →  {GREEN}{p_disc}/{total}{RESET} discovery  "
          f"(Δ {_delta(p_base, p_disc, total)}){DIM}{skip_note}{RESET}")

    # By category
    all_cats = sorted(set(s_base["cats"]) | set(s_disc["cats"]))
    print(f"\n{BOLD}By category:{RESET}")
    print(f"  {'Category':<22} {'Baseline':>12}  {'Discovery':>12}  {'Effect'}")
    print(f"  {'-'*22} {'-'*12}  {'-'*12}  {'-'*20}")
    for cat in all_cats:
        cb = s_base["cats"].get(cat, {"pass": 0, "total": 0})
        cd = s_disc["cats"].get(cat, {"pass": 0, "total": 0})
        pct_b = round(100 * cb["pass"] / cb["total"]) if cb["total"] else 0
        pct_d = round(100 * cd["pass"] / cd["total"]) if cd["total"] else 0
        print(f"  {cat:<22} {pct_color(pct_b)}{cb['pass']}/{cb['total']} ({pct_b}%){RESET:>4}  "
              f"{pct_color(pct_d)}{cd['pass']}/{cd['total']} ({pct_d}%){RESET:>4}  "
              f"{_arrow(pct_b, pct_d)}")

    # Per tool
    all_tools = sorted(set(s_base["tools"]) | set(s_disc["tools"]))
    print(f"\n{BOLD}Per-tool accuracy:{RESET}")
    print(f"  {'Tool':<42} {'Baseline':>10}  {'Discovery':>10}  {'Effect'}")
    print(f"  {'-'*42} {'-'*10}  {'-'*10}  {'-'*20}")
    for tool in all_tools:
        tb = s_base["tools"].get(tool, {"pass": 0, "total": 0})
        td = s_disc["tools"].get(tool, {"pass": 0, "total": 0})
        pct_b = round(100 * tb["pass"] / tb["total"]) if tb["total"] else 0
        pct_d = round(100 * td["pass"] / td["total"]) if td["total"] else 0
        flag = f"  {RED}⚠{RESET}" if pct_d < 80 else ""
        print(f"  {tool:<42} {pct_color(pct_b)}{tb['pass']}/{tb['total']} ({pct_b}%){RESET:>4}  "
              f"{pct_color(pct_d)}{td['pass']}/{td['total']} ({pct_d}%){RESET:>4}  "
              f"{_arrow(pct_b, pct_d)}{flag}")


def discovery_summary(discovery: list[dict]) -> dict:
    graded = scored(discovery)
    n = len(graded)
    passed = sum(1 for r in graded if r["passed"])
    steps = [r["steps"] for r in graded]
    paths: dict[str, int] = {}
    for r in graded:
        paths[r["discovery_path"]] = paths.get(r["discovery_path"], 0) + 1
    search_used = [r for r in graded if r["search_hit"] is not None]
    search_hits = sum(1 for r in search_used if r["search_hit"])
    toolset_graded = [r for r in graded if r["enabled_correct_toolset"] is not None]
    toolset_correct = sum(1 for r in toolset_graded if r["enabled_correct_toolset"])
    return {
        "fixtures": n,
        "skipped": sum(1 for r in discovery if r.get("skipped")),
        "passed": passed,
        "avg_steps": round(sum(steps) / n, 2) if n else 0,
        "max_steps_hit": sum(1 for r in graded if r.get("fail_reason") == "step_cap"),
        "path_distribution": paths,
        "search_used": len(search_used),
        "search_hit_rate": round(search_hits / len(search_used), 2) if search_used else None,
        "toolset_enable_graded": len(toolset_graded),
        "toolset_enable_correct": toolset_correct,
        "tokens": total_tokens(graded),
    }


def print_discovery_block(discovery: list[dict], baseline: list[dict] | None):
    s = discovery_summary(discovery)
    print(f"\n{BOLD}Discovery behaviour:{RESET}")
    print(f"  Avg steps to tool selection: {s['avg_steps']}  "
          f"(step-cap hit: {s['max_steps_hit']}/{s['fixtures']})")
    dist = "  ".join(f"{k}={v}" for k, v in sorted(s["path_distribution"].items()))
    print(f"  Discovery path: {dist}")
    if s["search_hit_rate"] is not None:
        print(f"  tool-search used in {s['search_used']} fixtures; "
              f"expected tool in results: {round(s['search_hit_rate']*100)}%")
    if s["toolset_enable_graded"]:
        print(f"  toolset-enable graded in {s['toolset_enable_graded']} fixtures; "
              f"correct toolset: {s['toolset_enable_correct']}/{s['toolset_enable_graded']}")
    d_tok = s["tokens"]
    print(f"  Tokens (discovery): {d_tok['input']:,} in / {d_tok['output']:,} out")
    if baseline:
        b_tok = total_tokens(baseline)
        print(f"  Tokens (baseline):  {b_tok['input']:,} in / {b_tok['output']:,} out")
        if b_tok["input"]:
            ratio = round(d_tok["input"] / b_tok["input"], 2)
            print(f"  Input-token ratio discovery/baseline: {ratio}x")

    skipped = [r for r in discovery if r.get("skipped")]
    if skipped:
        names = ", ".join(r["id"] for r in skipped)
        print(f"  {DIM}Skipped (expected tool not registered on this instance): {names}{RESET}")

    failed = [r for r in scored(discovery) if not r["passed"]]
    if failed:
        print(f"\n{BOLD}{RED}Failing in discovery mode:{RESET}")
        for r in failed:
            print(f"\n  [{r['id']}] {r['category']}  ({r.get('fail_reason')})")
            print(f"  {DIM}Prompt:{RESET}   {r['prompt'][:80]}")
            print(f"  {DIM}Expected:{RESET} {GREEN}{r['expected_tool']}{RESET}")
            print(f"  {DIM}Got:{RESET}      {RED}{r['selected_tool']}{RESET}")
            if r["meta_calls"]:
                trail = " → ".join(
                    f"{m['tool']}({json.dumps(m['input'], ensure_ascii=False)[:40]})"
                    for m in r["meta_calls"]
                )
                print(f"  {DIM}Trail:{RESET}    {trail}")
            if r.get("notes"):
                print(f"  {DIM}Notes:{RESET}    {r['notes'][:120]}")
    print(f"\n{'='*78}\n")


def print_single_mode(results: list[dict], mode: str):
    s = score(results)
    total = len(scored(results))
    passed = sum(1 for r in scored(results) if r["passed"])
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}Results: {mode} mode{RESET}")
    print(f"{'='*78}")
    pct = round(100 * passed / total) if total else 0
    skip_note = f"  ({skipped} skipped — tool not on this instance)" if skipped else ""
    print(f"  Overall: {pct_color(pct)}{passed}/{total} ({pct}%){RESET}{DIM}{skip_note}{RESET}")
    print(f"\n{BOLD}By category:{RESET}")
    for cat, c in sorted(s["cats"].items()):
        pct = round(100 * c["pass"] / c["total"]) if c["total"] else 0
        print(f"  {cat:<22} {pct_color(pct)}{c['pass']}/{c['total']} ({pct}%){RESET}")


def _delta(before: int, after: int, total: int) -> str:
    diff = after - before
    if diff > 0:
        return f"{GREEN}+{diff}{RESET}"
    if diff < 0:
        return f"{RED}{diff}{RESET}"
    return f"{DIM}0{RESET}"


def _arrow(pct_before: int, pct_after: int) -> str:
    diff = pct_after - pct_before
    if diff > 0:
        return f"{GREEN}↑ +{diff}pp{RESET}"
    if diff < 0:
        return f"{RED}↓ {diff}pp{RESET}"
    return f"{DIM}={RESET}"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
}


def skipped_result(fixture: dict, mode: str) -> dict:
    """A fixture whose expected tool is not registered on this instance is
    skipped, not failed — e.g. a dev-tools fixture on an instance without the
    SwagMcpDevTools bundle. Skipped fixtures are excluded from scoring."""
    return {
        "id": fixture["id"], "category": fixture.get("category", ""),
        "mode": mode, "prompt": fixture["prompt"],
        "expected_tool": fixture["expected_tool"], "selected_tool": None,
        "passed": False, "skipped": True,
        "skip_reason": "expected tool not registered on this instance",
    }


def run_baseline_pass(provider, client, fixtures, model, system_prompt, available_tools, endpoint=ADMIN):
    print(f"\n{BOLD}── Mode: baseline (full catalogue, single shot) ──{RESET}\n")
    session_id, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(session_id, endpoint=endpoint)
    mcp_tools = mcp_tools_list_all(session_id, endpoint=endpoint)
    tools_fn = tools_for_anthropic if provider == "anthropic" else tools_for_openai
    tools = tools_fn(mcp_tools)
    print(f"  Catalogue: {len(mcp_tools)} tools (all toolsets enabled)\n")

    results = []
    for i, fixture in enumerate(fixtures, 1):
        print(f"  [{i:02d}/{len(fixtures):02d}] {fixture['id']} ({fixture.get('category','')})")
        print(f"           {DIM}{fixture['prompt'][:65]}...{RESET}")
        if fixture["expected_tool"] not in available_tools:
            results.append(skipped_result(fixture, "baseline"))
            print(f"           {YELLOW}SKIP{RESET}  {fixture['expected_tool']} not registered")
            print()
            continue
        try:
            result = run_fixture_baseline(provider, client, tools, fixture, model, system_prompt)
            results.append(result)
            status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
            print(f"           {status}  selected={result['selected_tool'] or '(none)'}  {result['latency_s']}s")
        except Exception as e:
            print(f"           {RED}ERROR{RESET}: {e}")
            results.append({
                "id": fixture["id"], "category": fixture.get("category", ""),
                "mode": "baseline", "prompt": fixture["prompt"],
                "expected_tool": fixture["expected_tool"], "selected_tool": None,
                "passed": False, "error": str(e),
            })
        print()
    return results


def run_discovery_pass(provider, client, fixtures, model, system_prompt, default_max_steps,
                       available_tools, endpoint=ADMIN):
    print(f"\n{BOLD}── Mode: discovery (default surface + agentic loop) ──{RESET}\n")
    results = []
    for i, fixture in enumerate(fixtures, 1):
        max_steps = int(fixture.get("max_steps", default_max_steps))
        print(f"  [{i:02d}/{len(fixtures):02d}] {fixture['id']} ({fixture.get('category','')})")
        print(f"           {DIM}{fixture['prompt'][:65]}...{RESET}")
        if fixture["expected_tool"] not in available_tools:
            results.append(skipped_result(fixture, "discovery"))
            print(f"           {YELLOW}SKIP{RESET}  {fixture['expected_tool']} not registered")
            print()
            continue
        try:
            result = run_fixture_discovery(provider, client, fixture, model, system_prompt,
                                           max_steps, endpoint=endpoint)
            attempts = 1
            # Retry once on failure: gpt-4o is nondeterministic, so a single
            # borderline miss shouldn't flip CI red. A real regression fails
            # both attempts. Skips/errors are not retried.
            if not result["passed"]:
                print(f"           {YELLOW}retry{RESET} (first attempt selected="
                      f"{result['selected_tool'] or '(none)'})")
                retry = run_fixture_discovery(provider, client, fixture, model, system_prompt,
                                              max_steps, endpoint=endpoint)
                attempts = 2
                if retry["passed"]:
                    result = retry
            result["attempts"] = attempts
            results.append(result)
            status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
            path = result["discovery_path"]
            retry_note = f"  (attempts={attempts})" if attempts > 1 else ""
            print(f"           {status}  selected={result['selected_tool'] or '(none)'}  "
                  f"steps={result['steps']}  path={path}  {result['latency_s']}s{retry_note}")
        except Exception as e:
            print(f"           {RED}ERROR{RESET}: {e}")
            results.append({
                "id": fixture["id"], "category": fixture.get("category", ""),
                "mode": "discovery", "prompt": fixture["prompt"],
                "expected_tool": fixture["expected_tool"], "selected_tool": None,
                "passed": False, "error": str(e), "steps": 0, "meta_calls": [],
                "discovery_path": "none", "search_hit": None,
                "enabled_correct_toolset": None,
            })
        print()
    return results


def main():
    parser = argparse.ArgumentParser(description="Shopware MCP LLM Eval Runner (v2 discovery)")
    parser.add_argument("--provider", choices=["anthropic", "openai"],
                        default=os.environ.get("EVAL_PROVIDER", "anthropic"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--modes", default="baseline,discovery",
                        help="Comma-separated: baseline, discovery (default: both)")
    parser.add_argument("--max-steps", type=int, default=6,
                        help="Max assistant turns in discovery mode (per-fixture max_steps overrides)")
    parser.add_argument("--no-system-prompt", action="store_true",
                        help="Skip the MCP server system prompt (ad-hoc debugging)")
    parser.add_argument("--min-pass-rate", type=float,
                        default=float(os.environ.get("EVAL_MIN_PASS_RATE", "0.9")),
                        help="Min gating-mode pass rate for exit 0 (default 0.9; use 1.0 for strict)")
    parser.add_argument("--endpoint", choices=["admin", "store"], default="admin",
                        help="Which MCP endpoint to test (default admin). 'store' uses the Store API + UCP tools.")
    parser.add_argument("--fixtures",
                        help="Fixtures file (default: fixtures.yaml, or fixtures_store.yaml for --endpoint store)")
    parser.add_argument("--category", help="Run only fixtures of this category")
    parser.add_argument("--id", help="Run only this fixture ID")
    parser.add_argument("--output", help="Path to save JSON report")
    args = parser.parse_args()

    provider = args.provider
    model = args.model or os.environ.get("EVAL_MODEL") or PROVIDER_DEFAULTS[provider]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for m in modes:
        if m not in ("baseline", "discovery"):
            print(f"ERROR: unknown mode '{m}'", file=sys.stderr)
            sys.exit(1)

    endpoint = endpoint_by_name(args.endpoint)

    required = [("SW_BASE_URL", SW_BASE_URL)]
    if args.endpoint == "store":
        required.append(("SW_SC_ACCESS_KEY", SW_SC_ACCESS_KEY))
    else:
        required += [("SW_ACCESS_KEY", SW_ACCESS_KEY), ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)]
    required.append(("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY) if provider == "anthropic"
                    else ("OPENAI_API_KEY", OPENAI_API_KEY))
    for var, val in required:
        if not val:
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            sys.exit(1)

    if provider == "anthropic":
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    else:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

    default_fixtures = "fixtures_store.yaml" if args.endpoint == "store" else "fixtures.yaml"
    fixtures_path = Path(args.fixtures) if args.fixtures else Path(__file__).parent / default_fixtures
    all_fixtures = yaml.safe_load(fixtures_path.read_text())["fixtures"]
    if args.category:
        all_fixtures = [f for f in all_fixtures if f.get("category") == args.category]
    if args.id:
        all_fixtures = [f for f in all_fixtures if f["id"] == args.id]
    if not all_fixtures:
        print("No fixtures matched the filter.", file=sys.stderr)
        sys.exit(1)

    print(f"{BOLD}Shopware MCP LLM Eval (v2 discovery){RESET}")
    print(f"Server:   {SW_BASE_URL}  ({endpoint.name} endpoint)")
    print(f"Provider: {provider}")
    print(f"Model:    {model}")
    print(f"Modes:    {', '.join(modes)}")
    print(f"Fixtures: {len(all_fixtures)}")

    print("\nInitializing MCP session for system prompt...")
    session_id, server_instructions = mcp_init(endpoint=endpoint)
    if args.no_system_prompt:
        system_prompt = None
        print("System prompt: disabled (--no-system-prompt)")
    else:
        system_prompt = mcp_fetch_system_prompt(session_id, server_instructions, endpoint=endpoint)
        prompt_names = [line for line in system_prompt.split("\n") if line.startswith("# ")]
        print(f"System prompt: {len(prompt_names)} sections, {len(system_prompt)} chars")

    # The full catalogue on this instance. Fixtures whose expected tool is not
    # registered (e.g. a plugin bundle that isn't installed) are skipped, not
    # scored as failures.
    probe_sid, _ = mcp_init(endpoint=endpoint)
    enable_all_toolsets(probe_sid, endpoint=endpoint)
    available_tools = {t["name"] for t in mcp_tools_list_all(probe_sid, endpoint=endpoint)}
    absent = sorted({f["expected_tool"] for f in all_fixtures} - available_tools)
    if absent:
        print(f"Catalogue: {len(available_tools)} tools; will skip fixtures for absent: {', '.join(absent)}")

    results_baseline = None
    results_discovery = None
    if "baseline" in modes:
        results_baseline = run_baseline_pass(provider, client, all_fixtures, model, system_prompt,
                                             available_tools, endpoint=endpoint)
    if "discovery" in modes:
        results_discovery = run_discovery_pass(provider, client, all_fixtures, model,
                                               system_prompt, args.max_steps, available_tools, endpoint=endpoint)

    if results_baseline and results_discovery:
        print_comparison(results_baseline, results_discovery)
        print_discovery_block(results_discovery, results_baseline)
    elif results_discovery:
        print_single_mode(results_discovery, "discovery")
        print_discovery_block(results_discovery, None)
    elif results_baseline:
        print_single_mode(results_baseline, "baseline")

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output_path = args.output or str(BASE / "results" / f"eval-{provider}-{ts}.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "server": SW_BASE_URL,
        "provider": provider,
        "model": model,
        "modes": {},
        "fixtures": len(all_fixtures),
        "system_prompt": not args.no_system_prompt,
        "max_steps": args.max_steps,
    }
    if results_baseline is not None:
        report["modes"]["baseline"] = {
            "passed": sum(1 for r in scored(results_baseline) if r["passed"]),
            "failed": sum(1 for r in scored(results_baseline) if not r["passed"]),
            "skipped": sum(1 for r in results_baseline if r.get("skipped")),
            "tokens": total_tokens(scored(results_baseline)),
            "results": results_baseline,
        }
    if results_discovery is not None:
        report["modes"]["discovery"] = {
            "passed": sum(1 for r in scored(results_discovery) if r["passed"]),
            "failed": sum(1 for r in scored(results_discovery) if not r["passed"]),
            "skipped": sum(1 for r in results_discovery if r.get("skipped")),
            "results": results_discovery,
        }
        report["discovery_summary"] = discovery_summary(results_discovery)
    Path(output_path).write_text(json.dumps(report, indent=2))
    print(f"Report saved: {output_path}")

    # Gate: discovery mode is the v2 target behaviour; baseline is the
    # comparison reference and stays advisory when both run. Skipped fixtures
    # (tool absent on this instance) do not gate.
    #
    # The LLM eval is a quality signal against a nondeterministic model, so it
    # gates on a pass-rate threshold rather than a strict 100% — a couple of
    # borderline/flaky fixtures shouldn't flip CI red, but a real regression
    # (the rate collapsing) still fails. Each failed discovery fixture is also
    # retried once (see run_discovery_pass). Set --min-pass-rate 1.0 for strict.
    gating = scored(results_discovery if results_discovery is not None else results_baseline)
    passed = sum(1 for r in gating if r["passed"])
    rate = passed / len(gating) if gating else 1.0
    ok = rate >= args.min_pass_rate
    print(f"\nGate: {passed}/{len(gating)} = {round(rate*100)}% "
          f"(threshold {round(args.min_pass_rate*100)}%) → {'PASS' if ok else 'FAIL'}")
    if not ok:
        failed_ids = [r["id"] for r in gating if not r["passed"]]
        print(f"  below threshold; failing: {', '.join(failed_ids)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
