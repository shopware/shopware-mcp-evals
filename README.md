# shopware-mcp-evals

[![MCP Evals](https://github.com/shopware/shopware-mcp-evals/actions/workflows/mcp-evals.yml/badge.svg)](https://github.com/shopware/shopware-mcp-evals/actions/workflows/mcp-evals.yml)

Two-layer test suite for the Shopware MCP server. Runs against a live Shopware
instance over HTTP using integration access keys.

## Why this exists

The Shopware MCP server exposes ~22 tools to LLM clients (entity CRUD, system
config, merchant assistant, dev-tools logs, …). Two things can go wrong:

1. **The tool itself is broken**: wrong payload shape, missing fields, server
   error. A unit test in the Shopware repo cannot catch this end-to-end because
   it doesn't exercise the MCP transport, session, and JSON-RPC envelope.
2. **The tool description is ambiguous**: the LLM picks the wrong tool, or
   asks for clarification when it shouldn't, or fires the right tool with bad
   arguments. Static tests can't catch this; only running real prompts through
   a real model does.

This repo addresses both:

- **Layer 1 (functional)**: a bash script calls every tool with a minimal valid
  payload and checks the response structure. Mutating tools use `dryRun=true`.
  Catches transport / schema / handler regressions.
- **Layer 2 (LLM eval)**: natural-language prompts are sent to Claude (or
  GPT-4o) with the live tool list. Each prompt is run **twice**: once without
  the server's MCP system prompt and once with it. The comparison shows whether
  the system prompt actually helps tool selection, and which tool descriptions
  still cause confusion.

The output drives improvements to the `#[McpTool(description: '…')]`
attributes in the Shopware repo.

## Repository layout

```
.
├── README.md
├── AGENTS.md              # short brief for coding agents
├── .env.example           # required credentials and optional overrides
├── functional/
│   └── run.sh             # Layer 1: calls every tool with a dryRun payload
├── eval/
│   ├── run.py             # Layer 2: runs fixtures through an LLM
│   ├── fixtures.yaml      # natural-language prompts + expected tool
│   └── requirements.txt   # anthropic, openai, requests, pyyaml
└── results/               # JSON reports, gitignored
```

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

## Layer 1: Functional tests

Calls every registered tool with a minimal valid payload and verifies the
response structure. Mutating tools (`merchant-cart-checkout`, `merchant-product-create`,
`shopware-entity-upsert/delete`, `shopware-system-config-write`,
`shopware-order-state`, `shopware-theme-config`) all run with `dryRun=true`.

```bash
bash functional/run.sh

# skip the media-upload test (the only tool without a dryRun mode)
bash functional/run.sh --skip-media-upload
```

Pass / fail / skip per tool is printed to stdout. A JSON report is saved to
`results/functional-<timestamp>.json`. Exit code is non-zero if any tool fails.

## Layer 2: LLM eval

Loads `eval/fixtures.yaml`, fetches the live tool list and the MCP server's
context prompts, then runs each fixture twice (once **without** the system
prompt, once **with** it) and prints a side-by-side comparison of accuracy
deltas per fixture, per category, and overall.

```bash
# Anthropic (default), claude-sonnet-4-6
python eval/run.py

# OpenAI
python eval/run.py --provider openai --model gpt-4o

# Run only one category (unambiguous | disambiguation | chain)
python eval/run.py --category disambiguation

# Run a single fixture by ID
python eval/run.py --id disambig_count_vs_search

# Use a different model
python eval/run.py --model claude-opus-4-7

# Custom report path (default: results/eval-<provider>-<timestamp>.json)
python eval/run.py --output results/my-run.json
```

Exit code is 0 only if **all** fixtures pass the with-system-prompt run.

### Fixture categories

| Category | Purpose |
|---|---|
| `unambiguous` | One clear correct tool, sanity check that descriptions match obvious phrasings |
| `disambiguation` | Two or more tools could plausibly fit; the prompt must disambiguate via wording the description should disambiguate against |
| `chain` | Multi-step intents where the first tool call must be the right entry point |

27 fixtures in total. Add more by appending to `eval/fixtures.yaml` with the
required fields: `id`, `category`, `prompt`, `expected_tool`, optional `notes`.

## Scope

| Group | Tools |
|---|---|
| Core entity / config | `shopware-entity-{schema,search,read,aggregate,upsert,delete}`, `shopware-system-config-{read,write}`, `shopware-order-state`, `shopware-media-upload`, `shopware-theme-config` |
| Merchant assistant | `merchant-{customer-lookup,order-summary,cart-manage,cart-checkout,checkout-methods,product-create,storefront-search,bestseller-report,revenue-report}` |
| Dev tools | `swag-dev-tools-log-{search,stream}` |

Example bundle tools (`McpHelloWorld*`) are intentionally excluded.

## Improving tool descriptions

When a fixture fails the LLM eval:

1. Note the failing prompt and which tool was selected instead.
2. Edit the `#[McpTool(description: '...')]` attribute on the relevant PHP
   handler in the Shopware repo.
3. Restart the Shopware server (the tool list is read at startup).
4. Re-run `python eval/run.py --id <fixture-id>` to verify the fix.
5. Re-run the full eval to confirm no regressions in other fixtures.

## CI: pinned Shopware ref + drift detection

The eval workflow runs the suite against a Shopware checkout. To keep `main`
green when descriptions change upstream, it uses two gates:

**A. Pinned SHA for PRs and `main` pushes.** The file `shopware.sha` at repo
root holds the Shopware commit that PR/main runs check out. PRs are
reproducible, so upstream churn cannot flip the build red between commits.

**B. Snapshot-based drift detection.** After each run, the workflow snapshots
the live tool list to `tool-history/latest.json` and diffs it against the
committed baseline. If the snapshot changed, the LLM eval step is marked
advisory (`continue-on-error`) for that run, because descriptions have drifted
and failures don't represent a regression in the evals themselves. Functional
tests stay hard-failing in all cases.

| Trigger | Shopware ref used | If LLM eval fails |
|---|---|---|
| PR / push to main | pinned `shopware.sha` | hard fail (no drift expected) |
| `workflow_dispatch` | input ref, fallback `shopware.sha` | hard fail unless drift |
| Cron (daily 06:00 UTC) | `trunk` | advisory if drift, hard fail otherwise |

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
JSON-RPC `initialize` call before any tool invocation.

## Output format

Both layers write a JSON report to `results/`. Reports are gitignored.

`results/functional-<timestamp>.json`:
```json
{
  "timestamp": "...",
  "server": "...",
  "pass": 21, "fail": 0, "skip": 1, "total": 22,
  "tools": [{ "name": "...", "passed": true, "error": null }, ...]
}
```

`results/eval-<provider>-<timestamp>.json` contains both passes
(`results_without` and `results_with`), each with per-fixture
`{id, category, prompt, expected_tool, selected_tool, passed, latency_s, ...}`.
