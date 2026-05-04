#!/usr/bin/env python3
"""
Shopware MCP LLM Eval Runner

Always runs each fixture twice per provider:
  1. Without system prompt (bare tool descriptions only)
  2. With system prompt (MCP server instructions + all context prompts)

Then prints a side-by-side comparison showing the effect of the system prompt.

Usage:
    python eval/run.py                                  # Anthropic, default model
    python eval/run.py --provider openai --model gpt-4o
    python eval/run.py --category disambiguation
    python eval/run.py --id disambig_count_vs_search
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

load_env()

SW_BASE_URL = os.environ.get("SW_BASE_URL", "http://localhost:8000")
SW_ACCESS_KEY = os.environ.get("SW_ACCESS_KEY", "")
SW_SECRET_ACCESS_KEY = os.environ.get("SW_SECRET_ACCESS_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

AUTH_HEADERS = {
    "sw-access-key": SW_ACCESS_KEY,
    "sw-secret-access-key": SW_SECRET_ACCESS_KEY,
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------

def mcp_init() -> tuple[str, str]:
    """Initialize MCP session. Returns (session_id, server_instructions)."""
    resp = requests.post(
        f"{SW_BASE_URL}/api/_mcp",
        headers=AUTH_HEADERS,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-eval", "version": "1.0"},
            },
            "id": 1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    session_id = resp.headers.get("Mcp-Session-Id", "")
    if not session_id:
        raise RuntimeError("No Mcp-Session-Id in response headers")
    instructions = resp.json().get("result", {}).get("instructions", "")
    return session_id, instructions


def mcp_tools_list(session_id: str) -> list[dict]:
    headers = {**AUTH_HEADERS, "Mcp-Session-Id": session_id}
    resp = requests.post(
        f"{SW_BASE_URL}/api/_mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("result", {}).get("tools", [])


def mcp_fetch_system_prompt(session_id: str, server_instructions: str) -> str:
    """Fetch all MCP context prompts and combine with server instructions."""
    headers = {**AUTH_HEADERS, "Mcp-Session-Id": session_id}

    # List available prompts
    resp = requests.post(
        f"{SW_BASE_URL}/api/_mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "prompts/list", "params": {}, "id": 3},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_names = [p["name"] for p in resp.json().get("result", {}).get("prompts", [])]

    parts = []
    if server_instructions:
        parts.append(server_instructions.strip())

    for name in prompt_names:
        resp = requests.post(
            f"{SW_BASE_URL}/api/_mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "prompts/get", "params": {"name": name}, "id": 4},
            timeout=30,
        )
        resp.raise_for_status()
        messages = resp.json().get("result", {}).get("messages", [])
        for msg in messages:
            content = msg.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            if text.strip():
                parts.append(text.strip())

    return "\n\n---\n\n".join(parts)

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


def run_fixture_anthropic(client, tools: list[dict], fixture: dict, model: str, system_prompt: str | None) -> dict:
    prompt = fixture["prompt"]
    kwargs = dict(model=model, max_tokens=1024, tools=tools, messages=[{"role": "user", "content": prompt}])
    if system_prompt:
        kwargs["system"] = system_prompt

    t0 = time.time()
    response = client.messages.create(**kwargs)
    latency = round(time.time() - t0, 2)

    selected_tool, selected_input = None, {}
    for block in response.content:
        if block.type == "tool_use":
            selected_tool = block.name
            selected_input = block.input
            break

    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "prompt": prompt,
        "expected_tool": fixture["expected_tool"],
        "selected_tool": selected_tool,
        "selected_input": selected_input,
        "passed": selected_tool == fixture["expected_tool"],
        "latency_s": latency,
        "stop_reason": response.stop_reason,
        "notes": fixture.get("notes", ""),
    }


def run_fixture_openai(client, tools: list[dict], fixture: dict, model: str, system_prompt: str | None) -> dict:
    prompt = fixture["prompt"]
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    t0 = time.time()
    response = client.chat.completions.create(
        model=model, max_tokens=1024, tools=tools, tool_choice="auto", messages=messages,
    )
    latency = round(time.time() - t0, 2)

    selected_tool, selected_input = None, {}
    msg = response.choices[0].message
    if msg.tool_calls:
        call = msg.tool_calls[0]
        selected_tool = call.function.name
        try:
            selected_input = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            selected_input = {}

    return {
        "id": fixture["id"],
        "category": fixture.get("category", ""),
        "prompt": prompt,
        "expected_tool": fixture["expected_tool"],
        "selected_tool": selected_tool,
        "selected_input": selected_input,
        "passed": selected_tool == fixture["expected_tool"],
        "latency_s": latency,
        "stop_reason": response.choices[0].finish_reason,
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


def score(results: list[dict]) -> dict:
    """Return per-tool and per-category pass counts."""
    tools: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    for r in results:
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


def print_comparison(without: list[dict], with_prompt: list[dict]):
    s_without = score(without)
    s_with = score(with_prompt)

    total = len(without)
    p_without = sum(1 for r in without if r["passed"])
    p_with = sum(1 for r in with_prompt if r["passed"])

    print(f"\n{BOLD}{'='*78}{RESET}")
    print(f"{BOLD}Comparison: without system prompt  vs  with system prompt{RESET}")
    print(f"{'='*78}")
    print(f"  Overall: {GREEN}{p_without}/{total}{RESET} without  →  {GREEN}{p_with}/{total}{RESET} with  "
          f"(Δ {_delta(p_without, p_with, total)})")

    # By category
    all_cats = sorted(set(s_without["cats"]) | set(s_with["cats"]))
    print(f"\n{BOLD}By category:{RESET}")
    print(f"  {'Category':<22} {'Without':>12}  {'With':>12}  {'Effect'}")
    print(f"  {'-'*22} {'-'*12}  {'-'*12}  {'-'*20}")
    for cat in all_cats:
        cw = s_without["cats"].get(cat, {"pass": 0, "total": 0})
        cp = s_with["cats"].get(cat, {"pass": 0, "total": 0})
        pct_w = round(100 * cw["pass"] / cw["total"]) if cw["total"] else 0
        pct_p = round(100 * cp["pass"] / cp["total"]) if cp["total"] else 0
        print(f"  {cat:<22} {pct_color(pct_w)}{cw['pass']}/{cw['total']} ({pct_w}%){RESET:>4}  "
              f"{pct_color(pct_p)}{cp['pass']}/{cp['total']} ({pct_p}%){RESET:>4}  "
              f"{_arrow(pct_w, pct_p)}")

    # Per tool
    all_tools = sorted(set(s_without["tools"]) | set(s_with["tools"]))
    print(f"\n{BOLD}Per-tool accuracy:{RESET}")
    print(f"  {'Tool':<42} {'Without':>10}  {'With':>10}  {'Effect'}")
    print(f"  {'-'*42} {'-'*10}  {'-'*10}  {'-'*20}")
    for tool in all_tools:
        tw = s_without["tools"].get(tool, {"pass": 0, "total": 0})
        tp = s_with["tools"].get(tool, {"pass": 0, "total": 0})
        pct_w = round(100 * tw["pass"] / tw["total"]) if tw["total"] else 0
        pct_p = round(100 * tp["pass"] / tp["total"]) if tp["total"] else 0
        flag = f"  {RED}⚠{RESET}" if pct_p < 80 else ""
        print(f"  {tool:<42} {pct_color(pct_w)}{tw['pass']}/{tw['total']} ({pct_w}%){RESET:>4}  "
              f"{pct_color(pct_p)}{tp['pass']}/{tp['total']} ({pct_p}%){RESET:>4}  "
              f"{_arrow(pct_w, pct_p)}{flag}")

    # Failed cases (with prompt run — that's the "final" result)
    failed = [r for r in with_prompt if not r["passed"]]
    if failed:
        print(f"\n{BOLD}{RED}Still failing WITH system prompt:{RESET}")
        for r in failed:
            wo = next((x for x in without if x["id"] == r["id"]), None)
            wo_status = f"{GREEN}passed{RESET}" if wo and wo["passed"] else f"{RED}failed{RESET}"
            print(f"\n  [{r['id']}] {r['category']}  (without prompt: {wo_status})")
            print(f"  {DIM}Prompt:{RESET}   {r['prompt'][:80]}")
            print(f"  {DIM}Expected:{RESET} {GREEN}{r['expected_tool']}{RESET}")
            print(f"  {DIM}Got:{RESET}      {RED}{r['selected_tool']}{RESET}")
            if r.get("notes"):
                print(f"  {DIM}Notes:{RESET}    {r['notes'][:120]}")

    print(f"\n{'='*78}\n")


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


def run_pass(run_fixture_fn, client, tools, fixtures, model, system_prompt, label):
    """Run all fixtures for one pass (with or without system prompt)."""
    results = []
    sp_label = "with system prompt" if system_prompt else "without system prompt"
    print(f"\n{BOLD}── Pass: {sp_label} ──{RESET}\n")
    for i, fixture in enumerate(fixtures, 1):
        fid = fixture["id"]
        cat = fixture.get("category", "")
        print(f"  [{i:02d}/{len(fixtures):02d}] {fid} ({cat})")
        print(f"           {DIM}{fixture['prompt'][:65]}...{RESET}")
        try:
            result = run_fixture_fn(client, tools, fixture, model=model, system_prompt=system_prompt)
            results.append(result)
            status = f"{GREEN}PASS{RESET}" if result["passed"] else f"{RED}FAIL{RESET}"
            print(f"           {status}  selected={result['selected_tool'] or '(none)'}  {result['latency_s']}s")
        except Exception as e:
            print(f"           {RED}ERROR{RESET}: {e}")
            results.append({
                "id": fid, "category": cat, "prompt": fixture["prompt"],
                "expected_tool": fixture["expected_tool"], "selected_tool": None,
                "passed": False, "error": str(e),
            })
        print()
    return results


def main():
    parser = argparse.ArgumentParser(description="Shopware MCP LLM Eval Runner")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=os.environ.get("EVAL_PROVIDER", "anthropic"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--category", help="Run only fixtures of this category")
    parser.add_argument("--id", help="Run only this fixture ID")
    parser.add_argument("--output", help="Path to save JSON report")
    args = parser.parse_args()

    provider = args.provider
    model = args.model or os.environ.get("EVAL_MODEL") or PROVIDER_DEFAULTS[provider]

    required = [("SW_BASE_URL", SW_BASE_URL), ("SW_ACCESS_KEY", SW_ACCESS_KEY), ("SW_SECRET_ACCESS_KEY", SW_SECRET_ACCESS_KEY)]
    required.append(("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY) if provider == "anthropic" else ("OPENAI_API_KEY", OPENAI_API_KEY))
    for var, val in required:
        if not val:
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            sys.exit(1)

    if provider == "anthropic":
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        run_fixture_fn = run_fixture_anthropic
        tools_fn = tools_for_anthropic
    else:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        run_fixture_fn = run_fixture_openai
        tools_fn = tools_for_openai

    fixtures_path = Path(__file__).parent / "fixtures.yaml"
    all_fixtures = yaml.safe_load(fixtures_path.read_text())["fixtures"]
    if args.category:
        all_fixtures = [f for f in all_fixtures if f.get("category") == args.category]
    if args.id:
        all_fixtures = [f for f in all_fixtures if f["id"] == args.id]
    if not all_fixtures:
        print("No fixtures matched the filter.", file=sys.stderr)
        sys.exit(1)

    print(f"{BOLD}Shopware MCP LLM Eval{RESET}")
    print(f"Server:   {SW_BASE_URL}")
    print(f"Provider: {provider}")
    print(f"Model:    {model}")
    print(f"Fixtures: {len(all_fixtures)}  ×2 passes (without / with system prompt)")

    print("\nInitializing MCP session...")
    session_id, server_instructions = mcp_init()
    print(f"Session:  {session_id}")

    print("Fetching tools...")
    mcp_tools = mcp_tools_list(session_id)
    tools = tools_fn(mcp_tools)
    print(f"Tools:    {len(mcp_tools)}")

    print("Fetching system prompt (MCP context prompts)...")
    system_prompt = mcp_fetch_system_prompt(session_id, server_instructions)
    prompt_names = [line for line in system_prompt.split("\n") if line.startswith("# ")]
    print(f"Prompts:  {len(prompt_names)} sections, {len(system_prompt)} chars")

    # Run both passes
    results_without = run_pass(run_fixture_fn, client, tools, all_fixtures, model, None, "without")
    results_with = run_pass(run_fixture_fn, client, tools, all_fixtures, model, system_prompt, "with")

    print_comparison(results_without, results_with)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_path = args.output or str(BASE / "results" / f"eval-{provider}-{ts}.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": SW_BASE_URL,
        "provider": provider,
        "model": model,
        "fixtures": len(all_fixtures),
        "without_prompt": {
            "passed": sum(1 for r in results_without if r["passed"]),
            "failed": sum(1 for r in results_without if not r["passed"]),
            "results": results_without,
        },
        "with_prompt": {
            "passed": sum(1 for r in results_with if r["passed"]),
            "failed": sum(1 for r in results_with if not r["passed"]),
            "results": results_with,
        },
    }
    Path(output_path).write_text(json.dumps(report, indent=2))
    print(f"Report saved: {output_path}")

    all_passed = all(r["passed"] for r in results_with)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
