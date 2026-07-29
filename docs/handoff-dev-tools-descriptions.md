# Handoff: the dev-tools description cluster

Written 2026-07-29 against eval run [30435728030](https://github.com/shopware/shopware-mcp-evals/actions/runs/30435728030)
(commit `bd3a744`, `shopware.sha` = `8a390fc`). Read this before rewriting any
`swag-dev-tools-*` description — the headline number is misleading in a specific
way, and the first job is evidence-gathering, not prose.

## Where the descriptions live

**Not in this repo, and not in shopware/shopware.** They are
`#[McpTool(description: '…')]` attributes in the **SwagMcpDevTools bundle**
(composer package `swag/mcp-dev-tools`), which CI checks out to
`shopware/custom/bundles/SwagMcpDevTools`. Locally it is at
`~/Documents/Projects/shopware-trunk/custom/bundles/SwagMcpDevTools`, present but
**not installed** — `config/bundles.php` gates it on
`InstalledVersions::isInstalled('swag/mcp-dev-tools')`, and it is not in
`composer.json`. So a local eval run skips all 21 dev-tools fixtures unless you
install it first.

## The state of play

dev-tools reported **16/21 clean (76%)** with these five failing:

| fixture | expected | prompt |
|---|---|---|
| `list_skills` | `list-skills` | "What Shopware agent skills are available in this project?" |
| `list_skills_available` | `list-skills` | "Which agent skills can I load in here?" |
| `disambig_list_vs_load_skill` | `list-skills` | "Show me the catalogue of skills with a one-line description of each." |
| `load_skill_entity_definition` | `load-skill` | "Read me the instructions in the 'entity-definition' skill." |
| `chain_extensions_then_logs` | `list-extensions` | "Check whether SwagMcpDevTools is active, then look for any errors it has logged." |

**Four of the five are the `dev-skills` toolset**, and `list-skills` failed
**all three** of its fixtures while `load-skill` failed one of three
(`load_skill` and `load_skill_by_name` pass).

## Read this before you act on that

**None of the five failed on both models.** The By-owner table showed `—` in the
"Failed on every model" column for dev-tools, and the cross-model split reported
`both fail = 1` — which was `checkout_methods_shipping`, a merchant-tools fixture
since fixed. Every dev-tools failure is a single-model miss.

By the suite's own rule, documented at the top of `eval/compare_runs.py`, that
makes them capability gaps in the weaker model rather than description bugs:

```
both fail    -> the tool description is the problem. Actionable here.
only weak    -> a capability gap in the weaker model, not a description bug.
only strong  -> noise, almost always a flaky discovery run.
```

**But the same file states a second heuristic that disagrees:**

> a tool failing on all 3 of its prompts is a description to rewrite; failing on
> 1 of 3 is usually one awkward prompt

`list-skills` is 0/3. So the "3-of-3" rule says rewrite it and the "both-fail"
rule says do not. Resolving that tension is step 1, and it is resolved with
evidence, not by picking a rule.

## Step 1: get the discovery trails

The summary's `<details>` block only renders **both-fail** fixtures, so these
five have no trail in the job summary. Get them from the run artifact:

```bash
gh run download 30435728030 --repo shopware/shopware-mcp-evals --name <results-artifact>
python - <<'PY'
import json
d = json.load(open("results/eval-primary.json"))          # and eval-second-validator.json
ids = {"list_skills","list_skills_available","disambig_list_vs_load_skill",
       "load_skill_entity_definition","chain_extensions_then_logs"}
for r in d["modes"]["discovery"]["results"]:
    if r["id"] in ids:
        trail = " → ".join(f"{m['tool']}({(m.get('input') or {}).get('toolset') or (m.get('input') or {}).get('query','')})"
                           for m in r.get("meta_calls") or [])
        print(f"{r['id']:32} {str(r.get('selected_tool')):34} {r.get('fail_reason'):14} {trail}")
PY
```

`selected_tool` plus the trail splits the diagnosis two ways, and the fix is
completely different in each case:

**(a) The model reached `dev-skills` and picked the wrong tool inside it.**
Then it is a description overlap and the analysis below applies.

**(b) The model never enabled `dev-skills`** — it stalled with `no_tool_call` or
`step_cap`, or searched and gave up. Then rewriting either description changes
nothing, because neither was ever read. This is not hypothetical: in an earlier
run `meta_enable_dev_logs` failed exactly this way, with the trail
`shopware-toolsets-list → shopware-tool-search("dev logs toolset")` and no
enable. See "Step 3" for what actually fixes that.

Note `list_skills`' prompt is *"What Shopware agent skills are available in this
project?"* against a description opening *"List the Shopware Agent Skills shipped
in this project"* — a near-verbatim match. A near-verbatim match failing is
itself evidence for (b).

## Step 2: if it is (a) — the descriptions cross-reference each other

Three of the seven dev-tools descriptions name a sibling. Two of them are the
skills pair:

**`swag-dev-tools-list-skills`** (457 chars)
> List the Shopware Agent Skills shipped in this project under `.agents/skills/`
> (and any skills shipped by installed extensions). Returns each skill name,
> description, and source (core or extension). These skills are the authoritative
> source of truth for Shopware coding conventions — **use
> swag-dev-tools-load-skill to read one before generating or editing code.**
> Returns an empty list on installs that do not ship the source.

**`swag-dev-tools-load-skill`** (505 chars)
> Read the body of a Shopware Agent Skill by name **(as listed by
> swag-dev-tools-list-skills)**, e.g. "shopware-php-code" … Returns the SKILL.md
> content …

`list-skills` contains the words *read*, *one*, *skill* and the literal string
`swag-dev-tools-load-skill`. A model resolving *"Read me the instructions in the
'entity-definition' skill"* finds its target vocabulary inside the **wrong**
tool's description. The overlap is symmetric, which is why the pair trades
failures between runs.

Suggested direction, if (a) holds: strip the cross-reference from `list-skills`
— a tool description should not advertise a sibling's job — and make each
description lead with its own distinguishing action:

- `list-skills` → *what exists*: names, one-line descriptions, source. Returns an index.
- `load-skill` → *one skill's full body, by name*. Returns file content.

Do not simply delete "read" from `list-skills`; `disambig_list_vs_load_skill`
("catalogue of skills with a one-line description of each") needs `list-skills`
to still own *catalogue* and *one-line description*.

The third cross-reference, `scaffold` → `list-extensions` ("Resolve the target
extension first with swag-dev-tools-list-extensions"), is **fine** — both its
fixtures pass, and it describes a genuine ordering dependency rather than
duplicating a sibling's purpose. Leave it alone.

## Step 3: if it is (b) — this is the group-description gap, and it is measured

`shopware-toolsets-list` is the first call any client makes, and its payload
carries only group slugs and tool names — `{"name": "dev-skills", "title": "Dev
skills tools", "tools": [{"name": "swag-dev-tools-list-skills", "title": "List
Skills"}, …]}`. Nothing says what the group is *for*.

This was measured earlier in this repo's history, before dev-tools was installed
in the eval environment. Two arms, `gpt-5.4-mini`, two runs each, 69 runnable
admin fixtures, 138 graded decisions per arm:

| arm | pass | group routing | avg steps | step-cap hits |
|---|---|---|---|---|
| tool titles only | 94.2% | 119/120 (99.2%) | 3.03 | 2 |
| **+ one authored group description** | **99.3%** | **120/120** | 2.86 | **0** |

Every failure in the titles-only arm was the model *stalling after enabling the
correct group*, and group descriptions cleared all of them — at slightly **lower**
token cost, because fewer wasted steps. The experiment harness is
`/tmp/ab_groupdesc.py` (recreate it if gone: it wraps `eval.runner`'s `main()` and
patches `mcp_client.mcp_call` to inject a `description` per group).

This is also the follow-up that [shopware/shopware#18762](https://github.com/shopware/shopware/pull/18762)
explicitly deferred, in its own words:

> A group boundary statement (something like "log files on disk, not the
> `log_entry` table") is the one thing per-tool titles can't express. That is left
> for a follow-up, and only for the groups where cross-group confusion actually
> shows up.

The dev-* groups are where it shows up. Note the individual tool descriptions
**already carry** those boundary statements — `log-stream` and `log-search` both
say "DO NOT use this for the `log_entry` database table" — but they are invisible
at `toolsets-list` time, which is when the group is chosen.

## Constraints

- **21 dev-tools fixtures must keep passing.** The 16 currently clean ones are
  listed by running the snippet in Step 1 with the id filter removed. In
  particular `log-stream` vs `log-search` and `notifications` vs `log-stream`
  currently disambiguate correctly — their "DO NOT use this for…" clauses are
  load-bearing.
- **Every tool needs ≥3 fixtures differing in kind**, enforced by
  `tests/test_fixtures.py`. Do not add a fixture that merely rephrases another.
- **A description change means drift.** `tool-history/latest.json` is the
  committed baseline; the drift step warns when the live catalogue differs.
  Regenerate with `python -m eval.snapshot_tools --output tool-history/latest.json`
  and commit it alongside, or the next run cannot tell your change from a new one.
  Drift is currently already flagged against `8a390fc` and was consciously
  deferred — reconcile that first or you will not be able to attribute your own diff.
- **Do not rewrite a description to win one fixture.** That was the mistake
  `checkout_methods_shipping` nearly caused: the description matched the prompt
  almost word for word and the *fixture* was wrong. Check the fixture's intent
  (its `notes:` field) before touching prose.

## How to verify a change

```bash
# 1. install the bundle locally (it is gated on the composer package)
cd ~/Documents/Projects/shopware-trunk
composer config repositories.swag-mcp-dev-tools path custom/bundles/SwagMcpDevTools
composer require swag/mcp-dev-tools:@dev
# clear the HASHED cache dir, not var/cache/prod — Shopware ignores the latter
docker exec shopware-trunk-web-1 sh -c 'rm -rf /var/www/html/var/cache/prod_h*'

# 2. the local MCP rate limiter makes a 90-fixture run ~50 min of backoff sleep.
#    A local-only override, deleted afterwards:
#      config/packages/z-local.yaml -> shopware.api.rate_limiter.mcp_admin_api.enabled: false
#    then clear the hashed cache dir again.

# 3. run only the affected fixtures, both models, before and after
cd ~/Documents/Projects/shopware-mcp-evals
for m in gpt-5.4-mini gpt-4o-mini; do
  python -m eval.runner --modes discovery --provider openai --model $m \
    --category unambiguous --output "results/skills-$m.json"
done
python -m eval.compare_runs results/skills-gpt-5.4-mini.json results/skills-gpt-4o-mini.json \
  --catalogue tool-history/latest.json
```

Run each arm **twice** — `gpt-4o-mini` sits close enough to the 90% gate that a
single run moved 89% → 90% between two commits with no code change between them.
One run is not a result.

## The methodology point worth raising

`gpt-4o-mini` at a 90% gate flaps. On `b275e01` it scored 89% and failed the
build; on `bd3a744` it scored 90% and passed, with no change to descriptions or
fixtures in between. Its value is the **intersection** with the primary, and that
signal survives it scoring 89%. Consider lowering the second validator's own
threshold or making that step advisory, so a one-fixture wobble in the weaker
model stops gating on the stronger model's clean 94%.
