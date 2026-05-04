# shopware-mcp-evals

Test suite for the Shopware MCP server, run against a live instance.
Two layers: **functional** (shell, no LLM, all mutating tools dryRun-safe) and
**LLM eval** (Python, prompts → tool selection accuracy).

See `README.md` for the full picture and motivation; this file is the short
brief for coding agents.

## Setup

```bash
cp .env.example .env   # fill in credentials
pip install -r eval/requirements.txt
# or use the included venv: source .venv/bin/activate
```

## Running tests

```bash
# Layer 1 — functional (all tools, dryRun safe)
bash functional/run.sh
bash functional/run.sh --skip-media-upload

# Layer 2 — LLM eval. Each fixture is run twice: once without the MCP
# server's system prompt, once with it. The output is a side-by-side
# comparison showing the system prompt's effect on tool selection.
.venv/bin/python3 eval/run.py                          # Anthropic (default)
.venv/bin/python3 eval/run.py --provider openai        # OpenAI
.venv/bin/python3 eval/run.py --category disambiguation
.venv/bin/python3 eval/run.py --id disambig_count_vs_search
.venv/bin/python3 eval/run.py --output results/x.json  # custom report path
```

Exit code of `eval/run.py` is 0 only when every fixture passes the
with-system-prompt run.

## Auth

The MCP server at `SW_BASE_URL/api/_mcp` uses integration access keys — NOT OAuth.
Headers: `sw-access-key` + `sw-secret-access-key`.
The session must be initialized with `method: initialize` before any other call.

## Key files

| File | Purpose |
|---|---|
| `functional/run.sh` | Calls every tool with a minimal valid payload, checks response structure |
| `eval/fixtures.yaml` | Natural language prompts mapped to expected tool names |
| `eval/run.py` | Runs fixtures through an LLM, scores tool selection accuracy |
| `.env` | Local credentials (not committed) |
| `results/` | JSON reports from each run (not committed) |

## Scope

Tools under test: all `shopware-entity-*`, `shopware-system-config-*`, `shopware-order-state`, `shopware-media-upload`, `shopware-theme-config`, `merchant-*`, `swag-dev-tools-log-*`.
Example bundle tools (McpHelloWorld) are excluded.

## Improving tool descriptions

When an LLM eval fixture fails:
1. Note the failing prompt and which wrong tool was selected.
2. Edit the `#[McpTool(description: '...')]` PHP attribute in the Shopware repo.
3. Restart the Shopware server.
4. Re-run `eval/run.py --id <fixture-id>` to verify improvement.

## Adding fixtures

Add entries to `eval/fixtures.yaml`. Required fields: `id`, `category`,
`prompt`, `expected_tool`. Optional: `notes` (why this case is interesting).
Categories:

- `unambiguous` — one obvious tool; sanity check
- `disambiguation` — two or more plausible tools; description must disambiguate
- `chain` — multi-step intent; first tool call must be the right entry point

After editing a tool's PHP `#[McpTool(description: ...)]` in the Shopware repo,
restart the Shopware server before re-running the eval — the tool list is read
at MCP `initialize` time.
