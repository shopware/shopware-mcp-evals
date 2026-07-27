# shopware-mcp-evals

Test suite for the Shopware MCP server (**MCP Server v2**: dynamic tool
discovery), run against a live instance. Two layers: **functional** (Python,
no LLM, all mutating tools dryRun-safe) and **LLM eval** (Python, prompts →
tool selection accuracy).

See `README.md` for the full picture and motivation; this file is the short
brief for coding agents.

## Conventions

- **No shell scripts for test logic.** Both layers are Python; the functional
  runner (`functional/run.py`) reuses `mcp_client.py`. Don't add `.sh` runners —
  extend the Python runner or add a helper module. The only shell that belongs
  here is small CI glue under `functional/ci/*.sh`, which is shellcheck-linted.
- **Add unit tests for new logic.** New functional / eval / client behavior gets
  a pytest test under `tests/` — offline, faking the MCP server (see the existing
  tests). CI runs `ruff` + `pytest` + `shellcheck` on every push, so keep them green.
- **Three prompts per tool, differing in kind.** Every tool needs at least three
  fixtures: a canonical phrasing, a paraphrase avoiding the tool's own
  vocabulary, and a boundary case set against a sibling tool. Restating one
  sentence three ways proves nothing — the goal is to catch a description that
  only works for one lucky wording. `tests/test_fixtures.py` enforces the count
  (plus id/prompt uniqueness and toolset correctness) against
  `tool-history/latest.json`, so a new server-side tool fails the unit tests
  until it has prompts.
- **Two workflows.** `lint.yml` is the fast gate (ruff + format + pytest +
  shellcheck) and runs on every PR. `mcp-evals.yml` is the heavy one: it installs
  Shopware at the pinned `shopware.sha`, checks the plugin repos out at their
  **default branch** (so plugin churn can turn a run red without a change here),
  and runs the functional + LLM layers. The Store/UCP part is opt-in via the
  `run_store` dispatch input.

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
.venv/bin/python3 eval/run.py --provider github        # GitHub Models (free, needs GITHUB_TOKEN)
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

# Unit tests (offline, no server) — reporting, runner logic, throttle retry
.venv/bin/python3 -m pytest tests -q
```

**Gate: both models must reach 90%** in discovery mode — the primary and the
second validator each gate themselves, and `compare_runs.py --gate both` is the
consolidated verdict. Baseline is advisory when both modes run. Failed discovery
fixtures are retried once.

The rate is over fixtures that **ran**. Skipped ones (expected tool not
registered) never gate. Errored ones (server 500, throttling 429) are excluded
from the rate too — they are missing data, not wrong answers — but
`--max-error-rate` (default 0.1) fails the run as *invalid*. Do not fold errors
back into the rate: that reports a broken server as a bad model, and it once
turned an 89% run into a reported 53%.

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

Admin endpoint (`--endpoint admin`, the default): the 3 discovery meta-tools, all
`shopware-entity-*`, `shopware-system-config-*`, `shopware-order-state`,
`shopware-media-upload`, `shopware-theme-config`, `merchant-*`, and
`swag-dev-tools-*`. Example bundle tools (McpHelloWorld) are excluded.

Store API endpoint (`--endpoint store`): the meta-tools, `shopware-store-api-context`
and the `shopware-ucp-*` buyer-journey tools. The functional suite verifies
discovery mechanics only — it does not execute cart/checkout, which needs
provisioned state; tool *selection* for those is covered by the LLM eval.

Where the tools come from:

| Tools | Source |
|---|---|
| meta-tools, `shopware-entity-*`, `shopware-system-config-*`, `shopware-order-state`, `shopware-media-upload`, `shopware-theme-config`, `shopware-store-api-context` | Shopware core (`trunk`) |
| `merchant-*` | `shopware/SwagMcpMerchantTools` |
| `swag-dev-tools-*` | `shopware/SwagMcpDevTools` |
| `shopware-ucp-*` | `shopware/agentic-commerce` (`src/Ucp/Mcp/Tool`) |

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
