# shopware-mcp-evals

[![MCP Evals](https://github.com/shopware/shopware-mcp-evals/actions/workflows/mcp-evals.yml/badge.svg)](https://github.com/shopware/shopware-mcp-evals/actions/workflows/mcp-evals.yml)

Two-layer test suite for the Shopware MCP server. Runs against a live Shopware
instance over HTTP using integration access keys.

## Why this exists

The Shopware MCP server exposes ~30 tools to LLM clients (entity CRUD, system
config, merchant tools, dev tools, …). Since **MCP Server v2**
([shopware/shopware#17509](https://github.com/shopware/shopware/issues/17509))
**every** catalogue tool is **deferred**: a fresh session advertises only the
three discovery meta-tools, and the agent pulls in everything it needs on demand
via `shopware-tool-search` or by enabling toolsets with
`shopware-toolset-enable`. There is no default set of entity/entry tools — the
model must always discover and enable. The admin (`/api/_mcp`) and Store API
(`/store-api/_mcp`) endpoints share the same discovery mechanics; this suite
currently exercises the admin endpoint (Store API coverage is planned). Three
things can go wrong:

1. **The tool itself is broken**: wrong payload shape, missing fields, server
   error. A unit test in the Shopware repo cannot catch this end-to-end because
   it doesn't exercise the MCP transport, session, and JSON-RPC envelope.
2. **The discovery layer is broken**: a deferred tool leaks into the default
   surface, toolset enablement leaks across sessions, pagination loses tools,
   or tool-search fails to rank the right tool for an obvious query.
3. **The tool descriptions are ambiguous**: the LLM picks the wrong tool — or,
   new with v2, never *finds* the right tool because it can't discover it from
   the default surface. Static tests can't catch this; only running real
   prompts through a real model does.

This repo addresses all three:

- **Layer 1 (functional)**: a Python runner verifies the v2 discovery mechanics
  (default surface, toolsets, enable/isolation, pagination, tool-search) and
  then calls every tool with a minimal valid payload. Mutating tools use
  `dryRun=true`. Catches transport / schema / handler / discovery regressions.
- **Layer 2 (LLM eval)**: natural-language prompts are sent to Claude (or
  GPT-4o). Each fixture runs in two modes: **baseline** (full catalogue passed
  flat, the v1 situation) and **discovery** (default surface only; the runner
  executes meta-tool calls for real in an agentic loop). The comparison answers
  the core question: *does dynamic discovery make it harder for the model to
  pick the right tool, which discovery path does it take, and what does it cost
  in steps and tokens?*

The output drives improvements to the `#[McpTool(description: '…')]` and
`#[McpToolGroup('…')]` attributes in the Shopware repo.

## Repository layout

```
.
├── README.md
├── AGENTS.md              # short brief for coding agents
├── .env.example           # required credentials and optional overrides
├── shopware.sha           # pinned Shopware commit for reproducible CI runs
├── mcp_client.py          # shared MCP HTTP helpers (admin + store endpoints)
├── functional/
│   ├── run.py             # Layer 1: v2 discovery mechanics + per-tool dryRun calls (--endpoint admin|store)
│   ├── reporting.py       # pass/fail/skip harness + JSON report writer
│   └── ci/                # reusable shell helpers used by the workflow (shellcheck-linted)
├── eval/
│   ├── run.py             # Layer 2: baseline vs discovery LLM eval (--endpoint admin|store)
│   ├── snapshot_tools.py  # full-catalogue snapshot for drift detection
│   ├── fixtures.yaml      # admin natural-language prompts + expected tool
│   ├── fixtures_store.yaml # Store API / UCP prompts + expected tool
│   └── requirements.txt   # anthropic, openai, requests, pyyaml
├── tests/                 # pytest unit tests (reporting, runner logic, throttle retry)
├── ruff.toml              # Python lint config (eval + functional + mcp_client + tests)
├── requirements-dev.txt   # eval deps + pytest
├── tool-history/          # committed snapshot baseline (latest.json)
└── results/               # JSON reports, gitignored
```

Unit tests (offline, no server needed) cover the reporting harness, the runner's
verdict logic and full admin/store flows (driven through a fake MCP server), and
the client's HTTP 429 retry:

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

**Conventions:** both test layers are Python — don't add `.sh` runners (extend
`functional/run.py` or `mcp_client.py`; the only shell here is CI glue under
`functional/ci/`). New functional/eval/client logic ships with a pytest test
under `tests/`. `ruff`, `pytest`, and `shellcheck` run on every push.

## Setup

```bash
cp .env.example .env
# fill in SW_BASE_URL, SW_ACCESS_KEY, SW_SECRET_ACCESS_KEY
# add ANTHROPIC_API_KEY and/or OPENAI_API_KEY for Layer 2

pip install -r eval/requirements.txt
# or use the bundled venv: source .venv/bin/activate
```

Get an integration access key from
**Shopware Admin → Settings → System → Integrations**.

## The v2 discovery model (what the suite assumes)

- Every catalogue tool is deferred. A fresh session's `tools/list` contains
  only the three meta-tools: `shopware-tool-search`, `shopware-toolsets-list`,
  `shopware-toolset-enable`. Nothing else is advertised until the model enables
  a toolset (or surfaces a tool via `shopware-tool-search`).
- `tools/list` is cursor-paginated (`nextCursor`).
- `shopware-toolset-enable` grows the advertised list **per session**
  (persisted by `Mcp-Session-Id`) and emits `notifications/tools/list_changed`.
- **The allowlist stays the call boundary**: a deferred tool that is within the
  integration's allowlist is callable directly even when not advertised.
  Discovery narrows what is *advertised*, never what is *permitted*.

## Layer 1: Functional tests

First verifies the discovery mechanics:

- the default surface is exactly the non-deferred set (a deferred tool leaking
  into a fresh `tools/list` is a regression),
- `shopware-toolsets-list` returns the taxonomy with complete metadata,
- enabling a toolset returns `_meta.listChanged: true` and grows that session's
  paginated `tools/list` — and does **not** leak into other sessions,
- deferred tools stay directly callable without enabling,
- `shopware-tool-search` ranks deferred tools (with `score` / `matchedIn`) and
  caps `maxResults` at 20,
- unknown toolset names are rejected,
- after enabling all toolsets the paginated walk terminates without duplicates
  and contains the full expected catalogue.

Then calls every registered tool with a minimal valid payload and verifies the
response structure — on a session with **no** toolsets enabled, so every call
to a deferred tool doubles as a direct-callability assertion. Mutating tools
all run with `dryRun=true`.

```bash
python functional/run.py

# skip the media-upload test (the only tool without a dryRun mode)
python functional/run.py --skip-media-upload

# skip the SwagMcpDevTools assertions (instance without the dev-tools bundle)
python functional/run.py --skip-dev-tools

# Store API / UCP endpoint (needs SW_SC_ACCESS_KEY = a sales-channel access key)
python functional/run.py --endpoint store
```

Pass / fail / skip per check is printed to stdout. A JSON report is saved to
`results/functional-<timestamp>.json`. Exit code is non-zero if any check fails.

## Layer 2: LLM eval

Loads `eval/fixtures.yaml` and runs each fixture in up to two modes:

| Mode | Tool surface | Loop | Grading |
|---|---|---|---|
| `baseline` | full catalogue (all toolsets enabled), passed flat | single shot | first tool call == `expected_tool` |
| `discovery` | default advertised surface only | agentic, up to `--max-steps` turns; meta-tool calls (`shopware-tool-search`, `shopware-toolsets-list`, `shopware-toolset-enable`) are executed for real against the server and their results fed back; after an enable the tool list is re-fetched (simulating `tools/list_changed`) | first **non-meta** tool call == `expected_tool`; meta steps are free but counted |

The first non-meta tool call is never executed (same no-mutation policy as
before — grading is on selection). Discovery mode opens a **fresh MCP session
per fixture** because toolset enablement persists per session. For fixtures in
the `meta` category (where the meta-tool itself is the right answer), the first
tool call of any kind is graded.

```bash
# Both modes, Anthropic (default), claude-sonnet-4-6
python eval/run.py

# OpenAI
python eval/run.py --provider openai --model gpt-4o

# Discovery mode only, higher step cap
python eval/run.py --modes discovery --max-steps 8

# Run only one category (unambiguous | disambiguation | chain | meta | discovery)
python eval/run.py --category disambiguation

# Run a single fixture by ID
python eval/run.py --id disambig_count_vs_search

# Without the MCP system prompt (ad-hoc debugging)
python eval/run.py --no-system-prompt

# Custom report path (default: results/eval-<provider>-<timestamp>.json)
python eval/run.py --output results/my-run.json
```

Exit code is 0 if the **discovery** run meets the pass-rate threshold
(`--min-pass-rate`, default 0.9); baseline is the comparison reference and stays
advisory when both modes run. Because the LLM is nondeterministic, each failed
discovery fixture is **retried once** (only a double failure counts), and the
threshold tolerates a couple of borderline fixtures so a single flaky prompt
can't flip CI red — while a real regression (the rate collapsing) still fails.
Use `--min-pass-rate 1.0` for strict all-must-pass. Skipped fixtures (expected
tool not registered on the instance) never gate.

### Discovery metrics

Per fixture (in the JSON report and the console summary):

| Metric | Meaning |
|---|---|
| `steps` | assistant turns until a non-meta tool was selected |
| `discovery_path` | `direct` (no meta calls), `search`, `toolsets`, `mixed`, or `none` |
| `search_hit` | when tool-search was used: did its results contain `expected_tool`? |
| `enabled_correct_toolset` | when toolset-enable was used and the fixture declares `expected_toolset`: was the right toolset enabled? |
| `tokens` | summed input/output tokens across all turns |
| `fail_reason` | `wrong_tool`, `no_tool_call`, or `step_cap` |

The summary block also reports the input-token ratio discovery/baseline — the
"cost of discovery" number: how much context the default-surface approach saves
(or spends in extra turns) against advertising the full catalogue up front.

### Fixture categories

| Category | Purpose |
|---|---|
| `unambiguous` | One clear correct tool, sanity check that descriptions match obvious phrasings |
| `disambiguation` | Two or more tools could plausibly fit; the prompt must disambiguate via wording the description should disambiguate against |
| `chain` | Multi-step intents where the first (non-meta) tool call must be the right entry point |
| `meta` | The discovery meta-tool itself is the correct answer |
| `discovery` | Deep-deferred tools with no default-surface sibling; probes search/toolset ranking end to end |

33 fixtures in total. Add more by appending to `eval/fixtures.yaml` with the
required fields: `id`, `category`, `prompt`, `expected_tool`; optional:
`expected_toolset` (for deferred tools), `max_steps`, `notes`.

## Scope

| Group | Tools |
|---|---|
| Discovery meta-tools | `shopware-tool-search`, `shopware-toolsets-list`, `shopware-toolset-enable` |
| Core entity / config | `shopware-entity-{schema,search,read,aggregate,upsert,delete}`, `shopware-system-config-{read,write}`, `shopware-order-state`, `shopware-media-upload`, `shopware-theme-config` |
| Merchant tools | `merchant-{customer-lookup,order-summary,cart-manage,cart-checkout,checkout-methods,product-create,storefront-search,bestseller-report,revenue-report}` |
| Dev tools | `swag-dev-tools-{log-search,log-stream,list-extensions,list-skills,load-skill,notifications,scaffold}` |

Example / demo tools (`SwagMcpExampleBundle`) and the `SwagMcpAdminUsers` plugin
are not installed in CI, so they are outside the tested catalogue.

The **Store API MCP endpoint** (`/store-api/_mcp`) is covered experimentally: the
UCP buyer-journey tools (`shopware-ucp-*`) and `shopware-store-api-context` come
from the `SwagAgenticCommerce` plugin. It uses the same discovery mechanics as
admin but authenticates with a sales-channel access key (`SW_SC_ACCESS_KEY`) plus
a context token. Run it with:

```bash
python functional/run.py --endpoint store               # Layer 1 (discovery mechanics + context)
python eval/run.py --endpoint store --modes discovery   # Layer 2 (UCP tool selection)
```

In CI it is opt-in (the `run_store` dispatch input installs `SwagAgenticCommerce`)
and the store LLM eval is advisory.

### Coverage

All 30 admin-catalogue tools have both a functional assertion and at least one
LLM fixture. Functional assertions that depend on shop data (a product, order,
customer, or storefront sales channel) run against CI-seeded demo data and skip
gracefully if that data is absent locally. The `--skip-media-upload` and
`--skip-dev-tools` flags skip those groups locally; CI runs both.

## Improving tool descriptions

When a fixture fails the LLM eval:

1. Note the failing prompt, the mode, and the meta-call trail (discovery mode
   prints `search(...) → enable(...) → wrong-tool`).
2. Edit the `#[McpTool(description: '…')]` — or, for discovery failures, the
   `#[McpToolGroup('…')]` / `meta: ['deferred' => …]` — attribute on the
   relevant PHP handler in the Shopware repo.
3. Restart the Shopware server (the tool list is read at startup).
4. Re-run `python eval/run.py --id <fixture-id>` to verify the fix.
5. Re-run the full eval to confirm no regressions in other fixtures.

## CI: pinned Shopware ref + drift detection

The eval workflow runs the suite against a Shopware checkout. To keep `main`
green when descriptions change upstream, it uses two gates:

**A. Pinned SHA for PRs and `main` pushes.** The file `shopware.sha` at repo
root holds the Shopware commit that PR/main runs check out (now a `trunk`
commit — MCP v2 discovery is merged). PRs are reproducible, so upstream churn
cannot flip the build red between commits. The plugin checkouts
(`SwagMcpDevTools`, `SwagMcpMerchantTools`) default to their
`feat/mcp-tool-groups-deferral` branches and can be overridden via the
`dev_tools_ref` / `merchant_tools_ref` dispatch inputs.

**B. Snapshot-based drift detection.** After each run, the workflow snapshots
the live catalogue to `tool-history/latest.json` and diffs it against the
committed baseline. The v2 snapshot captures the **default surface**, the
**toolset taxonomy**, and the **full catalogue** (taken after enabling every
toolset), so drift now also covers default-surface and toolset-membership
changes. If the snapshot changed, the LLM eval step is marked advisory
(`continue-on-error`) for that run. Functional tests stay hard-failing in all
cases.

| Trigger | Shopware ref used | If LLM eval fails |
|---|---|---|
| PR / push to main | pinned `shopware.sha` | hard fail (no drift expected) |
| `workflow_dispatch` | input ref, fallback `shopware.sha` | hard fail unless drift |
| Cron (daily 06:00 UTC) | `trunk` | advisory if drift, hard fail otherwise |

> **Note:** MCP v2 discovery is now merged into Shopware `trunk`, so cron runs
> against `trunk` no longer report drift by design — a drift report from cron
> now means real upstream description churn worth reconciling.

### Reconciliation flow (when cron goes red or you want a newer SHA)

When upstream Shopware ships description changes, the daily cron run surfaces
the drift in its workflow summary. To adopt the new descriptions, open a PR
**against this repo (`shopware/shopware-mcp-evals`)**:

1. Pick the Shopware commit you want to pin to (usually current `trunk` HEAD).
2. Trigger the workflow manually (Actions → MCP Evals → Run workflow) with
   `shopware_ref` set to that commit. Wait for it to finish. Even if the LLM
   eval fails, the **artifacts** are what matter.
3. Download the `tool-history-<run-id>` artifact and copy its
   `latest.json` over `tool-history/latest.json` in your working tree.
4. Update `shopware.sha` to the same commit.
5. Run the eval locally (or open a draft PR) to see which fixtures fail at the
   new SHA. For each:
   - if the new description is better, edit `eval/fixtures.yaml` to match the
     new wording, **or**
   - if the new description regressed, open a separate PR upstream against
     `shopware/shopware` to fix it, and hold this reconciliation until that
     lands.
6. Commit `shopware.sha`, `tool-history/latest.json`, and any
   `eval/fixtures.yaml` updates in a single PR to `shopware/shopware-mcp-evals`.
   Once it lands, drift goes back to zero and PR/main runs hard-gate again.

This is the **only** way the pinned SHA changes. Never auto-bump from CI.

### Tool description history

`tool-history/latest.json` doubles as the committed baseline and the audit
trail. `git log -p tool-history/latest.json` pairs each eval pass/fail flip
with the upstream description edit that caused it. Generate one locally with:

```bash
python eval/snapshot_tools.py --output tool-history/latest.json
```

## Auth

The MCP endpoint at `SW_BASE_URL/api/_mcp` uses Shopware integration access
keys via `sw-access-key` / `sw-secret-access-key` headers, **not** OAuth. The
integration key establishes a server-to-server session; every run starts with a
JSON-RPC `initialize` call before any tool invocation. The `Mcp-Session-Id`
response header identifies the session and scopes toolset enablement.

## Output format

Both layers write a JSON report to `results/`. Reports are gitignored.

`results/functional-<timestamp>.json`:
```json
{
  "timestamp": "...",
  "server": "...",
  "pass": 40, "fail": 0, "skip": 1, "total": 41,
  "tools": [{ "tool": "...", "label": "...", "status": "pass" }, ...]
}
```

`results/eval-<provider>-<timestamp>.json` contains a `modes` object with
`baseline` and/or `discovery` blocks (per-fixture
`{id, category, mode, expected_tool, selected_tool, passed, steps, meta_calls,
discovery_path, search_hit, enabled_correct_toolset, tokens, latency_s, ...}`)
plus a `discovery_summary` aggregate (pass rate, average steps, path
distribution, search-hit rate, token totals).
