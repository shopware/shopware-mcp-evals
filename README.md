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
(`/store-api/_mcp`) endpoints share the same discovery mechanics, and this suite
exercises both. Three things can go wrong:

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
  GPT-4o) in **discovery** mode — the default advertised surface only, with the
  runner executing meta-tool calls for real in an agentic loop. It answers the
  core question: *can the model find the right tool through dynamic discovery,
  which discovery path does it take, and what does it cost in steps and tokens?*

The output drives improvements to the `#[McpTool(description: '…')]` and
`#[McpToolGroup('…')]` attributes in the Shopware repo.

## Repository layout

```
.
├── README.md
├── AGENTS.md              # short brief for coding agents
├── .env.example           # required credentials and optional overrides
├── pyproject.toml         # package metadata; `pip install -e .` makes the imports work
├── shopware.sha           # pinned Shopware commit for reproducible CI runs
├── mcp_client.py          # shared MCP HTTP helpers (admin + store endpoints)
├── ownership.py           # tool name → owning repository, and what a failure there costs
├── functional/
│   ├── runner.py          # Layer 1: v2 discovery mechanics + per-tool dryRun calls (--endpoint admin|store)
│   ├── checks.py          # the per-tool assertion table (payloads, labels, prerequisites)
│   ├── reporting.py       # pass/fail/skip harness + JSON report writer
│   └── ci/                # reusable shell helpers used by the workflow (shellcheck-linted)
├── eval/
│   ├── runner.py          # Layer 2: discovery-mode LLM eval (--endpoint admin|store)
│   ├── scoring.py         # results → counts, rates and the gate verdict (pure)
│   ├── report.py          # terminal rendering of a run (pure of scoring)
│   ├── compare_runs.py    # primary vs second validator; the both-fail set to act on
│   ├── summary.py         # one GitHub job summary for every run in the job
│   ├── snapshot_tools.py  # full-catalogue snapshot for drift detection
│   ├── fixtures.yaml      # admin natural-language prompts + expected tool
│   ├── fixtures_store.yaml # Store API / UCP prompts + expected tool
│   └── requirements.txt   # anthropic, openai, requests, pyyaml
├── tests/                 # pytest unit tests (reporting, runner logic, throttle retry)
├── ruff.toml              # Python lint config
├── requirements-dev.txt   # eval deps + pytest + pytest-cov + ruff
├── tool-history/          # committed snapshot baseline (latest.json)
└── results/               # JSON reports, gitignored
```

The repo is an installable package, which is what lets every module import every
other one by name (`from mcp_client import ...`) from anywhere, and lets the tests
import their subject directly. Install it once, in editable mode:

```bash
pip install -r requirements-dev.txt
pip install -e . --no-deps
```

`--no-deps` because `eval/requirements.txt` and `requirements-dev.txt` are the
single source of truth for versions; `pyproject.toml` declares none.

Unit tests (offline, no server needed) cover the gate arithmetic, the scoring and
rendering, the transport (pagination, SSE, throttle retry), the check table, and
full admin/store flows driven through a fake MCP server.

**Branch coverage is gated at 85%** (`fail_under` in `pyproject.toml`, so a local
`pytest --cov` enforces the same number CI does). The suite sits at ~87%, and the
floor deliberately keeps a couple of points of slack: the same tests report a
handful of statements differently on linux than on darwin, and a floor with no
slack would fail on that alone.

```bash
pip install -r requirements-dev.txt
pip install -e . --no-deps
pytest tests -q
pytest tests -q --cov   # enforces the 85% floor and lists what is uncovered
```

Branch coverage rather than statement coverage, because an `if` whose false side
never runs still counts as a covered statement — and the untaken side is where
the skip reasons, the error budget and the gate thresholds live.

**Conventions:** both test layers are Python — don't add `.sh` runners (extend
`functional/runner.py` or `mcp_client.py`; the only shell here is CI glue under
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
python -m functional.runner

# skip the media-upload test (the only tool without a dryRun mode)
python -m functional.runner --skip-media-upload

# skip the SwagMcpDevTools assertions (instance without the dev-tools bundle)
python -m functional.runner --skip-dev-tools

# Store API / UCP endpoint (needs SW_SC_ACCESS_KEY = a sales-channel access key)
python -m functional.runner --endpoint store
```

Pass / fail / skip per check is printed to stdout. A JSON report is saved to
`results/functional-<timestamp>.json`. Exit code is non-zero if any check fails.

## Layer 2: LLM eval

Loads `eval/fixtures.yaml` and runs each fixture in **discovery** mode:

| Mode | Tool surface | Loop | Grading |
|---|---|---|---|
| `discovery` | default advertised surface only | agentic, up to `--max-steps` turns; meta-tool calls (`shopware-tool-search`, `shopware-toolsets-list`, `shopware-toolset-enable`) are executed for real against the server and their results fed back; after an enable the tool list is re-fetched (simulating `tools/list_changed`) | first **non-meta** tool call == `expected_tool`; meta steps are free but counted |

The first non-meta tool call is never executed (grading is on selection, so
nothing mutates). Discovery opens a **fresh MCP session per fixture** because
toolset enablement persists per session. For fixtures in the `meta` category
(where the meta-tool itself is the right answer), the first tool call of any kind
is graded. Each failing fixture is retried once, so a reported failure is two
lost attempts.

<details>
<summary>There used to be a second <code>baseline</code> mode. Why it went.</summary>

`baseline` enabled every toolset and graded the first call of a single request,
as a v1 comparison reference. But it left the discovery meta-tools in the
catalogue it handed the model, and graded the first call **without** exempting
them — so a model that followed the server's own `instructions` and called
`shopware-toolsets-list` was scored as picking the wrong tool. On the last run
before removal, **40 of its 42 failures on the primary model were that
artifact**, which made its per-tool `Effect` column read as evidence for
progressive disclosure when it was really measuring the grading difference
between the two modes. The remaining two were genuine, and the per-tool table
that replaced it still surfaces that class.

`--modes baseline` now fails with an explanation rather than silently doing
something else.
</details>

```bash
# Anthropic (default), claude-sonnet-4-6
python -m eval.runner

# OpenAI — gpt-5.4-mini is the CI primary and the openai default
python -m eval.runner --provider openai --model gpt-5.4-mini

# Second validator in CI — an older-generation model on the same fixtures. A
# fixture both models miss points at the tool description; one both pass is noise.
python -m eval.runner --provider openai --model gpt-4o-mini

# GitHub Models is also supported (free, OpenAI-compatible, auth via GITHUB_TOKEN
# with `models: read`, and its catalogue carries non-OpenAI publishers for genuine
# cross-provider signal — but no Anthropic models).
#
# It is NOT used for CI: the free tier cannot carry a full sweep. Even at the old
# 45 fixtures x 2 modes it throttled *every* one, answering HTTP 403 "Too many
# requests" rather
# than 429. Lowering concurrency does not help against a per-day quota. Fine for
# small local runs:
python -m eval.runner --provider github --id disambig_count_vs_search
python -m eval.runner --provider github --category meta

# Higher step cap
python -m eval.runner --max-steps 8

# More fixtures at once. The default of 4 is what an instance with the MCP rate
# limiter still enabled can take; CI disables it and uses 12.
python -m eval.runner --discovery-concurrency 12

# Run only one category (unambiguous | disambiguation | chain | meta | discovery)
python -m eval.runner --category disambiguation

# Run a single fixture by ID
python -m eval.runner --id disambig_count_vs_search

# Without the MCP system prompt (ad-hoc debugging)
python -m eval.runner --no-system-prompt

# Custom report path (default: results/eval-<provider>-<timestamp>.json)
python -m eval.runner --output results/my-run.json
```

### Gate policy

**The primary must reach 90%; the second validator 85%.** Each gates itself, and
`eval/compare_runs.py --gate both --min-pass-rate 0.9 --min-pass-rate-second 0.85`
is the consolidated verdict.

The thresholds differ because the two models are there for different reasons. The
second validator's worth is the **intersection** with the primary — a fixture both
models miss points at the tool description, one only it misses is its own
capability gap — and that signal survives it scoring a few points lower. At a
shared 90% it flapped instead: it scored 89% on one commit and 90% on the next
with no change to descriptions or fixtures in between, and later failed a run at
88% core while the primary was clean. 85% still catches a collapse, which is what
the gate is for. Re-measure before swapping either model.

**Core is additionally gated on its own denominator.** The admin suite spans
four repositories, so one aggregate rate lets a core regression hide behind
clean plugin numbers: nine core misses out of 90 fixtures still reads 90% PASS
as long as merchant-tools and dev-tools are perfect — backwards, since the
plugin numbers are the ones we can afford to lose. `--min-core-pass-rate`
(default: same as `--min-pass-rate`) is checked over core fixtures alone. The
default is deliberately not stricter: the win is core getting its own
denominator, and raising the bar is a decision to make once the per-owner rates
have been observed, not a number invented up front.

The rate is computed over **fixtures that actually ran**. Two categories are
held out, for different reasons:

| Excluded | Why | How it can still fail the run |
|---|---|---|
| **skipped** — expected tool not registered on this instance (e.g. dev-tools fixtures without the bundle) | not applicable here | never |
| **errored** — the request never reached the model (server 500, throttling 429) | missing data, not a wrong answer | `--max-error-rate` (default 0.1) fails the run as *invalid* |

Keeping those separate matters: folding errors into the rate reports a broken
server as a bad model. One run read as 53% when 18 of its 21 "failures" were
the Shopware container erroring — the rate over fixtures that ran was 89%. A
run is now failed for a quality regression **or** for being untrustworthy, and
the output says which.

Because the LLM is nondeterministic, each failed fixture is **retried once**
(only a double failure counts), and the thresholds tolerate a couple of
borderline fixtures so one flaky prompt can't flip CI red — while a real
regression still fails. Use `--min-pass-rate 1.0` for strict all-must-pass.

### When it runs

`mcp-evals.yml` runs nightly at 06:00 UTC and on manual `workflow_dispatch`. On a
pull request it is **opt-in**: the job only runs when the PR carries the
**`run-evals`** label, because a run installs Shopware and spends real OpenAI
credit while most changes here are already covered by `lint.yml`. Adding the label
starts a run, and it re-runs on each subsequent push for as long as the label
stays. An unlabelled PR shows the job as skipped.

`eval_provider` is a dropdown with one option, `openai`, because that is the only
provider with a key in this repo. The runner supports `--provider anthropic` and
the workflow already passes `ANTHROPIC_API_KEY` through, so enabling it is adding
that secret and one line to the input's `options`.

### The CI job summary

`mcp-evals.yml` runs `eval/runner.py` three times — admin primary, admin second
validator, Store/UCP advisory. The two admin arms run **concurrently in one step**
(they are independent, and sequentially they were two thirds of the job's wall
clock); the Store arm follows. Each writes a JSON
verdict row (`--summary-row`, labelled with `--suite-label`) instead of markdown,
and a final `eval/summary.py` step renders them as **one** suite-labelled table
followed by the cross-model comparison:

```
| Suite                    | Provider | Model         | Pass rate | Graded | Errors | Throttled | Gate            |
| admin · primary          | openai   | gpt-5.4-mini  | 92%       | 90     | 0      | 0         | PASS            |
| admin · second validator | openai   | gpt-4o-mini   | 91%       | 90     | 0      | 0         | PASS            |
| store · UCP              | openai   | gpt-5.4-mini  | 90%       | 42     | 0      | 0         | PASS (advisory) |
```

A second table splits the same runs by **owning repository**, because "admin at
92%" spans core, a first-party bundle and two optional plugins, and those
failures are not worth the same:

| Owner | Prefix | Repository | Enforcement |
|---|---|---|---|
| `core · discovery` | the 3 meta-tools | `shopware/shopware` | core gate + suite rate |
| `core` | `shopware-*` | `shopware/shopware` | core gate + suite rate |
| `dev-tools` | `swag-dev-tools-*` | `SwagMcpDevTools` (bundle) | suite rate |
| `merchant-tools` | `merchant-*` | `SwagMcpMerchantTools` (plugin) | suite rate |
| `agentic-commerce` | `shopware-ucp-*` | `shopware/agentic-commerce` | advisory |

Attribution is by tool-name prefix (`ownership.py`), longest match first —
`shopware-ucp-*` is agentic-commerce, not core. A fixture is attributed by its
`expected_tool`: the description that should have won is the one under test, so
a cross-boundary miss counts against the tool that lost. Because prefixes are a
convention, `tests/test_ownership.py` fails when a tool in the snapshot matches
none of them, rather than silently filing it under core.

The discovery meta-tools are reported apart — a break there makes every tool
undiscoverable — but counted inside core for gating: nine fixtures is too small
a denominator to gate on, one miss swings it 11 points.

The both-fail table names the **confusion pair**, not just the expected tool —
knowing `swag-dev-tools-list-skills` failed tells you a description is wrong;
knowing the model reached for `swag-dev-tools-load-skill` instead tells you what
to rewrite it against:

```
| Owner     | Expected tool              | Fixture               | primary `gpt-5.4-mini` picked | second `gpt-4o-mini` picked | Note |
| dev-tools | swag-dev-tools-list-skills | list_skills_available | swag-dev-tools-load-skill     | shopware-tool-search        |      |
```

Rows are ordered core-first, then by how many prompts the tool lost: owner
decides urgency, count decides whether it is a description to rewrite (3/3) or
one awkward prompt (1/3).

`fail_reason` shows in the Note column only when the picked columns can't convey
it (`no_tool_call`, `step_cap`); `wrong_tool` is already implied by the picks.
Run `eval/summary.py` locally against a `results/rows/` directory to preview it.

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

The summary block also reports the run's total input/output tokens, which is the
"cost of discovery" number to watch between commits: extra turns spent finding a
tool show up here before they show up in the pass rate.

### Fixture categories

| Category | Purpose |
|---|---|
| `unambiguous` | One clear correct tool, sanity check that descriptions match obvious phrasings |
| `disambiguation` | Two or more tools could plausibly fit; the prompt must disambiguate via wording the description should disambiguate against |
| `chain` | Multi-step intents where the first (non-meta) tool call must be the right entry point |
| `meta` | The discovery meta-tool itself is the correct answer |
| `discovery` | Deep-deferred tools with no default-surface sibling; probes search/toolset ranking end to end |

**90 admin fixtures** (`eval/fixtures.yaml`) and **42 store fixtures**
(`eval/fixtures_store.yaml`) — at least **3 prompts per tool**.

The three prompts must differ *in kind*, not just in wording: a canonical
phrasing, a real-user paraphrase that avoids the tool's own vocabulary, and a
boundary case framed next to a sibling tool. Three restatements of one sentence
only prove the model can match one phrasing; the point is to find descriptions
that work for a single lucky wording and no other.

Add more by appending with the required fields: `id`, `category`, `prompt`,
`expected_tool`; optional: `expected_toolset` (for deferred tools),
`acceptable_tools`, `max_steps`, `notes`.

`tests/test_fixtures.py` enforces the invariants without needing a server —
unique ids and prompts, known categories, `expected_toolset` present on
non-meta fixtures and matching the committed snapshot, and every tool in
`tool-history/latest.json` carrying at least 3 prompts. That last one is the
drift guard: a tool added server-side fails the unit tests until someone writes
prompts for it.

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
from the `shopware/agentic-commerce` plugin. It uses the same discovery mechanics as
admin but authenticates with a sales-channel access key (`SW_SC_ACCESS_KEY`) plus
a context token. Run it with:

```bash
python -m functional.runner --endpoint store               # Layer 1 (discovery mechanics + context)
python -m eval.runner --endpoint store   # Layer 2 (UCP tool selection)
```

The `shopware-ucp-*` tools come from the **`shopware/agentic-commerce`** plugin
(`src/Ucp/Mcp/Tool`, plugin class `Swag\AgenticCommerce\SwagAgenticCommerce`),
which pulls the UCP protocol layer in via `ucp-php-sdk/symfony-bundle` — public
on Packagist, so composer resolves it with no extra wiring.

In CI this suite **runs by default**; pass `run_store=false` to skip it. The
store LLM eval stays advisory until it has a track record — the store functional
suite does gate.

Both wiring it up (private checkout, `composer require`) and running it are
**non-fatal**: on failure the workflow records `STORE_PLUGIN_OK=false`, emits a
`::warning::` and a job-summary note naming the cause, skips the store steps,
and lets the admin evals finish. It is never swallowed silently — a silent skip
is precisely how these 42 fixtures went unrun for so long.

The usual cause is credentials: `shopware/agentic-commerce` is private, so
`PLUGINS_PAT` needs read access to it. (Composer resolution is fine —
`dev-trunk` is `6.7.x-dev` and the plugin accepts `~6.7`.)

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
4. Re-run `python -m eval.runner --id <fixture-id>` to verify the fix.
5. Re-run the full eval to confirm no regressions in other fixtures.

## CI: pinned Shopware ref + drift detection

The eval workflow runs the suite against a Shopware checkout. To keep `main`
green when descriptions change upstream, it uses two gates:

**A. Pinned SHA for PRs and `main` pushes.** The file `shopware.sha` at repo
root holds the Shopware commit that PR/main runs check out (now a `trunk`
commit — MCP v2 discovery is merged). PRs are reproducible, so upstream churn
cannot flip the build red between commits. The plugin checkouts
`SwagMcpDevTools` and `SwagMcpMerchantTools` deliberately do **not** pin: each
tracks its own repository default branch, so runs always exercise the latest
plugin code. The trade-off is that plugin-side churn can turn a run red without
a change here — pin a single run via the `dev_tools_ref` / `merchant_tools_ref`
dispatch inputs. `shopware/agentic-commerce` tracks its default branch too, and
is checked out unless `run_store=false`.

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
python -m eval.snapshot_tools --output tool-history/latest.json
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

`results/eval-<provider>-<timestamp>.json` contains a `modes` object with a
`discovery` block (per-fixture
`{id, category, mode, expected_tool, selected_tool, passed, steps, meta_calls,
discovery_path, search_hit, enabled_correct_toolset, tokens, latency_s, ...}`)
plus a `discovery_summary` aggregate (pass rate, average steps, path
distribution, search-hit rate, token totals).
