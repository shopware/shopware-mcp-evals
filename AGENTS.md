# shopware-mcp-evals

Test suite for the Shopware MCP server (**MCP Server v2**: dynamic tool
discovery), run against a live instance. Two layers: **functional** (Python,
no LLM, all mutating tools dryRun-safe) and **LLM eval** (Python, prompts →
tool selection accuracy).

See `README.md` for the full picture and motivation; this file is the short
brief for coding agents.

## Conventions

- **No shell scripts for test logic.** Both layers are Python; the functional
  runner (`functional/runner.py`) reuses `mcp_client.py`. Don't add `.sh` runners —
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
  and runs the functional + LLM layers. The Store/UCP part runs by default too;
  skip it with `run_store=false`. If `agentic-commerce` fails to resolve against
  the pinned Shopware ref, the store steps skip with a warning rather than
  taking down the admin evals.
- **The eval workflow is opt-in on PRs.** It runs nightly and on
  `workflow_dispatch` unconditionally, but on a pull request only when the PR
  carries the **`run-evals`** label — it costs a Shopware install and real OpenAI
  credit, and most changes here are covered by `lint.yml`. Add the label to start
  a run; it then re-runs on each push while the label stays. Unlabelled PRs show
  the job as skipped rather than not appearing at all.
- **`openai` is the only working provider.** The repo has `OPENAI_API_KEY` and
  `PLUGINS_PAT`, no `ANTHROPIC_API_KEY`, so `eval_provider` is a one-option
  dropdown. The runner itself supports `--provider anthropic` and the workflow
  already passes the secret through — enabling it is adding the secret plus one
  line to the input's `options`.
- **One job summary, rendered once.** Each `eval/runner.py` invocation writes a
  JSON verdict row (`--summary-row`, `--suite-label`, `--advisory`) and the
  final `eval/summary.py` step renders every row, plus the cross-model
  comparison, into `GITHUB_STEP_SUMMARY`. Don't append markdown to the step
  summary from the runners: three processes appending cannot form one table,
  which is how the summary ended up as three one-row tables repeating each
  other's numbers, with an unlabelled Store row at the bottom.
  `compare_runs.py` still prints its full report (including the per-model rate
  table) to stdout for local use — only the CI render is centralized, and it
  drops the rates because `summary.py` already shows them per suite.
- **Attribute failures to a repository.** `ownership.py` maps a tool name to
  the codebase that owns it (core / dev-tools / merchant-tools /
  agentic-commerce) by longest-matching prefix, and core is gated on its own
  denominator so a core regression cannot be averaged away by clean plugin
  numbers. Add new prefixes there, not inline: `tests/test_ownership.py` fails
  when a tool in `tool-history/latest.json` matches none of them, which is the
  point — an unattributed tool would silently be filed under core.
- **Pin the tooling, range the runtime.** `eval/requirements.txt` keeps ranges
  (with major caps) so CI tests against current SDKs; `requirements-dev.txt`
  pins `ruff` exactly and bounds `pytest`. A linter release adds rules and
  turns CI red with no code change, which is not a failure worth having.

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
pip install -e . --no-deps   # makes `from mcp_client import ...` resolve anywhere
# or use the included venv: source .venv/bin/activate
```

## Running tests

```bash
# Layer 0 — static: description checks over the committed catalogue snapshot.
# No server, no model, no tokens. Advisory: always exits 0.
python -m toollint

# Layer 1 — functional: v2 discovery mechanics + per-tool dryRun-safe calls
python -m functional.runner
python -m functional.runner --skip-media-upload
python -m functional.runner --skip-dev-tools

# Layer 2 — LLM eval. Discovery mode only: default surface + agentic meta-tool
# loop. There was a `baseline` mode (full catalogue, single shot); it was removed
# because it graded the first call without exempting the discovery meta-tools it
# had put in the catalogue, so 40 of its 42 failures were the model correctly
# calling shopware-toolsets-list. `--modes baseline` now errors with that reason.
.venv/bin/python3 -m eval.runner                          # Anthropic
.venv/bin/python3 -m eval.runner --provider openai        # OpenAI
.venv/bin/python3 -m eval.runner --provider github        # GitHub Models (free, needs GITHUB_TOKEN)
.venv/bin/python3 -m eval.runner --max-steps 8
.venv/bin/python3 -m eval.runner --discovery-concurrency 12  # 4 by default; CI disables the MCP rate limiter and uses 12
.venv/bin/python3 -m eval.runner --category disambiguation
.venv/bin/python3 -m eval.runner --id disambig_count_vs_search
.venv/bin/python3 -m eval.runner --no-system-prompt       # ad-hoc, skip system prompt
.venv/bin/python3 -m eval.runner --output results/x.json  # custom report path
.venv/bin/python3 -m eval.runner --triage                 # re-run ONLY the failures under
                                                          # the isolated + full arms.
                                                          # In CI: nightly, or the `triage`
                                                          # workflow_dispatch input.

# Cost of a run vs the previous one, per fixture. Warns, never gates.
.venv/bin/python3 -m eval.cost_drift --current results/eval-primary.json \
                                     --previous baseline/eval-primary.json

# Snapshot the full catalogue for drift detection
.venv/bin/python3 eval/snapshot_tools.py --output tool-history/latest.json

# Store API / UCP endpoint (needs SW_SC_ACCESS_KEY = a sales-channel access key)
python -m functional.runner --endpoint store
.venv/bin/python3 -m eval.runner --endpoint store

# Unit tests (offline, no server) — reporting, runner logic, throttle retry
.venv/bin/python3 -m pytest tests -q
.venv/bin/python3 -m pytest tests -q --cov   # enforces the 90% branch-coverage floor
```

> **Measured under the new definition of `passed`** (tool executed, result
> asserted, recovery allowed): primary **97%** (93/96), second validator **95%**
> (91/96), store **64%** (29/45). The admin thresholds were left at 0.90/0.85 —
> both clear them with room, and raising them off a single run is what produced
> the 89%/90% flapping this repo already documents.
>
> The store suite's 64% is a real finding, not a threshold problem: every UCP
> tool is unsafe to execute, so that suite is graded on selection alone, and its
> failures cluster on `order-get`/`checkout-get`. It is advisory, so it shows up
> as a red annotation on an otherwise green job.

**Gate: the primary must reach 90%, the second validator 85%.** Each gates
itself, and `compare_runs.py --gate both --min-pass-rate 0.9
--min-pass-rate-second 0.85` is the consolidated verdict. The weaker model gets
slack on purpose: its worth is the intersection with the primary, and at a shared
90% it flapped (89% on one commit, 90% on the next, nothing changed in between).
Failed fixtures are retried once, so a reported failure is two lost attempts.

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
| `functional/runner.py` | v2 discovery mechanics + per-tool minimal-payload calls (`--endpoint admin\|store`) |
| `functional/reporting.py` | Reusable pass/fail/skip harness + JSON report writer |
| `eval/fixtures_store.yaml` | Store API / UCP buyer-journey fixtures |
| `eval/fixtures.yaml` | Natural language prompts mapped to expected tool names |
| `eval/runner.py` | Discovery-mode LLM eval: finds the tool, executes it, asserts the result, allows recovery |
| `eval/scoring.py` | Results → counts, rates and the gate verdict. Pure, and what the gate is decided by |
| `toolclass.py` | **Read before touching execution.** May a tool be called, and how to make it safe (read-only / dry-runnable / unsafe / unclassified) |
| `toollint.py` | Layer 0 static description checks; advisory, runs in lint.yml |
| `eval/assertions.py` | `expect_result` tiers, and the line between a call the server rejected and one that returned nothing |
| `eval/tool_scorecard.py` | Per-tool recall, **precision**, F1, confusion pairs. The half a pass rate cannot show |
| `eval/cost.py` / `eval/cost_drift.py` | What a run costs, and whether that moved |
| `pricing.yaml` | $/1M per model. Hand-maintained; `tests/test_cost.py` fails on an unpriced default model |
| `eval/report.py` | Terminal rendering of a run, kept apart from the scoring it renders |
| `functional/checks.py` | The per-tool assertion table: payload, label, prerequisites |
| `eval/snapshot_tools.py` | Full-catalogue snapshot (default surface + toolsets + tools) |
| `eval/drift.py` | Names what moved between two snapshots; drives the drift summary and the nightly reconciliation PR |
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

Do not start from the failing prompt — start from where the failure *is*. Three
different problems produce an identical failure in the gating arm, and each
wants a different edit:

1. **Run `--triage`** (or read the "Where the failures are" table in the job
   summary). It re-runs the failures with only their own toolset enabled, then
   with the whole catalogue enabled:
   - fails both → the tool's own `#[McpTool(description:)]`
   - passes isolated, fails full → a **cross-group collision**; fix the pair,
     not the tool. The scorecard's `steals_from` column names the other half
   - passes both → the **discovery layer**: `#[McpToolGroup]` description, or
     tool-search ranking (check `search_rank`)
2. **Check the per-tool scorecard** for precision. A tool with high recall and
   low precision is over-broad and is quietly taking its siblings' prompts —
   that is invisible in the pass rate and is usually the real bug.
3. Edit the PHP attribute in the Shopware repo, restart the server, and re-run
   `eval/runner.py --id <fixture-id>`.

`python -m toollint` is free and catches some of this before a run: a
description that never says *when* to call the tool fires on half the catalogue.

## Adding fixtures

Add entries to `eval/fixtures.yaml`. Required fields: `id`, `category`,
`prompt`, `expected_tool`. Optional: `expect_result` (assertion tier — defaults
to `accepted`; see the header of fixtures.yaml for why everything is on that
default for now), `expected_toolset` (for deferred tools —
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

### Negative fixtures

`category: negative` + `expect_no_tool: true`, no `expected_tool`. The right
answer is that nothing applies, which is the only way to catch an over-broad
description directly rather than waiting for it to steal a sibling's fixture.

Two rules, both learned the hard way:

- **No legitimate first step.** "Export all orders to CSV and email it" is not a
  valid negative — fetching the orders is a reasonable opening move, so a model
  calling `entity-search` is behaving correctly and the fixture would punish it.
  Keep them external or infrastructural.
- **Plausible and adjacent.** "What is the weather" measures nothing. The point
  is a request a shop admin would really make, next to a group that might bite.

Meta calls are free — searching, finding nothing, then declining is the
behaviour under test and passes. Running out of steps does **not** pass: that is
a model still rummaging, not one that concluded. `notes` must say what
capability would answer the prompt, because the fixture becomes a false
accusation the moment the server grows it.
