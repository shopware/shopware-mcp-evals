# shopware-mcp-evals

Test suite for the Shopware MCP server (**MCP Server v2**: dynamic tool
discovery), run against a live instance. Two layers: **functional** (shell,
no LLM, all mutating tools dryRun-safe) and **LLM eval** (Python, prompts →
tool selection accuracy).

See `README.md` for the full picture and motivation; this file is the short
brief for coding agents.

## v2 invariants (read before touching the runners)

- A fresh session advertises only **non-deferred** tools. Deferred tools hide
  until their toolset is enabled — but stay directly callable if allowlisted.
  The allowlist is the call boundary; advertising is not.
- `tools/list` is cursor-paginated — always walk `nextCursor` (see
  `mcp_tools_list_all`), never assume one page.
- The three meta-tools (`META_TOOLS` in `mcp_client.py`):
  `shopware-tool-search`, `shopware-toolsets-list`, `shopware-toolset-enable`.
- `shopware-toolset-enable` persists per `Mcp-Session-Id`. Discovery-mode eval
  therefore opens a **fresh session per fixture** so enablement can't leak.

## Setup

```bash
cp .env.example .env   # fill in credentials
pip install -r eval/requirements.txt
# or use the included venv: source .venv/bin/activate
```

## Running tests

```bash
# Layer 1 — functional: v2 discovery mechanics + per-tool dryRun-safe calls
python functional/run.py
python functional/run.py --skip-media-upload
python functional/run.py --skip-dev-tools

# Layer 2 — LLM eval. Each fixture runs in baseline mode (full catalogue,
# single shot) and discovery mode (default surface + agentic meta-tool loop).
.venv/bin/python3 eval/run.py                          # both modes, Anthropic
.venv/bin/python3 eval/run.py --provider openai        # OpenAI
.venv/bin/python3 eval/run.py --modes discovery --max-steps 8
.venv/bin/python3 eval/run.py --category disambiguation
.venv/bin/python3 eval/run.py --id disambig_count_vs_search
.venv/bin/python3 eval/run.py --no-system-prompt       # ad-hoc, skip system prompt
.venv/bin/python3 eval/run.py --output results/x.json  # custom report path

# Snapshot the full catalogue for drift detection
.venv/bin/python3 eval/snapshot_tools.py --output tool-history/latest.json

# Store API / UCP endpoint (needs SW_SC_ACCESS_KEY = a sales-channel access key)
python functional/run.py --endpoint store
.venv/bin/python3 eval/run.py --endpoint store --modes discovery
```

Exit code of `eval/run.py` is 0 when the **discovery** run meets `--min-pass-rate`
(default 0.9); failed discovery fixtures are retried once. Baseline is advisory
when both modes run. Fixtures whose expected tool isn't registered are skipped.

## Auth

The MCP server at `SW_BASE_URL/api/_mcp` uses integration access keys — NOT OAuth.
Headers: `sw-access-key` + `sw-secret-access-key`.
The session must be initialized with `method: initialize` before any other call;
the `Mcp-Session-Id` response header scopes toolset enablement.

## Key files

| File | Purpose |
|---|---|
| `mcp_client.py` | Shared MCP HTTP helpers + `ADMIN`/`STORE` endpoints; session, paginated `tools/list`, toolsets, enable-all, `META_TOOLS`/`DEFAULT_SURFACE` |
| `functional/run.py` | v2 discovery mechanics + per-tool minimal-payload calls (`--endpoint admin\|store`) |
| `functional/reporting.py` | Reusable pass/fail/skip harness + JSON report writer |
| `eval/fixtures_store.yaml` | Store API / UCP buyer-journey fixtures |
| `eval/fixtures.yaml` | Natural language prompts mapped to expected tool names |
| `eval/run.py` | Baseline vs discovery LLM eval, scores tool selection accuracy |
| `eval/snapshot_tools.py` | Full-catalogue snapshot (default surface + toolsets + tools) |
| `shopware.sha` | Pinned Shopware commit for reproducible CI |
| `tool-history/latest.json` | Committed drift baseline |
| `.env` | Local credentials (not committed) |
| `results/` | JSON reports from each run (not committed) |

## Scope

Tools under test: the 3 discovery meta-tools, all `shopware-entity-*`,
`shopware-system-config-*`, `shopware-order-state`, `shopware-media-upload`,
`shopware-theme-config`, `merchant-*`, and `swag-dev-tools-*`.
Example bundle tools (McpHelloWorld) and the Store API MCP endpoint are excluded.

## Improving tool descriptions / groups

When an LLM eval fixture fails:
1. Note the failing prompt, the mode, and (discovery mode) the meta-call trail.
2. Edit the `#[McpTool(description: '...')]`, `#[McpToolGroup('...')]`, or
   `meta: ['deferred' => ...]` PHP attribute in the Shopware repo.
3. Restart the Shopware server.
4. Re-run `eval/run.py --id <fixture-id>` to verify improvement.

## Adding fixtures

Add entries to `eval/fixtures.yaml`. Required fields: `id`, `category`,
`prompt`, `expected_tool`. Optional: `expected_toolset` (for deferred tools —
grades whether the right toolset was enabled), `max_steps` (per-fixture
discovery step cap), `notes`. Categories:

- `unambiguous` — one obvious tool; sanity check
- `disambiguation` — two or more plausible tools; description must disambiguate
- `chain` — multi-step intent; first (non-meta) tool call must be the right entry point
- `meta` — the discovery meta-tool itself is the correct answer (first call of any kind graded)
- `discovery` — deep-deferred tool with no default-surface sibling; probes search/toolset ranking

After editing a tool's PHP attributes in the Shopware repo, restart the
Shopware server before re-running — the tool list is read at MCP `initialize`
time.
