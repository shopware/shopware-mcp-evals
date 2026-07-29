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

# Snapshot the full catalogue for drift detection
.venv/bin/python3 eval/snapshot_tools.py --output tool-history/latest.json

# Store API / UCP endpoint (needs SW_SC_ACCESS_KEY = a sales-channel access key)
python -m functional.runner --endpoint store
.venv/bin/python3 -m eval.runner --endpoint store

# Unit tests (offline, no server) — reporting, runner logic, throttle retry
.venv/bin/python3 -m pytest tests -q
.venv/bin/python3 -m pytest tests -q --cov   # enforces the 80% branch-coverage floor
```

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
| `eval/runner.py` | Baseline vs discovery LLM eval, scores tool selection accuracy |
| `eval/scoring.py` | Results → counts, rates and the gate verdict. Pure, and what the gate is decided by |
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

When an LLM eval fixture fails:
1. Note the failing prompt, the mode, and (discovery mode) the meta-call trail.
2. Edit the `#[McpTool(description: '...')]`, `#[McpToolGroup('...')]`, or
   `meta: ['deferred' => ...]` PHP attribute in the Shopware repo.
3. Restart the Shopware server.
4. Re-run `eval/runner.py --id <fixture-id>` to verify improvement.

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
