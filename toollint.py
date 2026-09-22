#!/usr/bin/env python3
"""Static checks over the tool catalogue — no server, no model, no tokens.

Layer 2 costs money and needs a live Shopware instance. Some description
problems do not: they are visible in the committed snapshot. This runs in
lint.yml, in under a second, on every push.

What is checked here was chosen by measuring the catalogue, not by listing
plausible smells. Two checks that seemed obvious were cut for the same reason:

  * "parameter has no description" fires on 102 of 102 parameters, and
  * "string parameter has no enum/format/pattern" on 74 of 74.

Both are uniform, so as per-tool findings they would emit 30 rows that all say
one thing about how the server is written. They are reported once, as catalogue
facts, where they are informative instead of noise. This server documents its
parameters in prose inside the tool description, which is a defensible choice —
the lint's job is to say so once, not to relitigate it thirty times.

Uniform is not the same as harmless, though, and the eval measured the
difference: `shopware-entity-aggregate.aggregations` is a `{"type": "string"}`
with no description, no examples and no pattern, and the contract it omits ("a
JSON array of aggregation definitions") is the one the server rejects calls
for. Three core fixtures on run 33598354019 picked that tool correctly and
still failed, because the argument could not be formed from what the schema
says. So the two counts are *budgeted* rather than gated: a committed ceiling
they may fall below freely and may not rise above. That keeps one fact in the
report instead of thirty findings, while stopping the thirty-first undescribed
parameter from arriving unnoticed.

Description similarity is deliberately NOT a standalone finding. Measured
against the collisions the per-tool scorecard actually confirmed, it ranks 5 of
6 inside the top 15% of pairs — better than chance, but the *top* of the list is
dominated by pairs that never collide (the two most similar descriptions in the
catalogue, `merchant-bestseller-report` and `merchant-revenue-report`, have
never been confused once). Shipped as a prediction it would mostly cry wolf. It
is exposed as `similarity()` so a *confirmed* collision can be annotated with
it, which is a question it can answer: a confirmed pair that reads similarly
needs rewording, while a confirmed pair that already reads differently is being
confused semantically and rewording will not help.

Usage:
    python -m toollint --snapshot tool-history/latest.json
"""

import argparse
import json
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import cast

from eval.result_schema import (
    CatalogueFacts,
    LintBudget,
    LintReport,
    SimilarPair,
    Snapshot,
    ToolDef,
    ToolLintEntry,
    as_object,
)

# Words carrying no signal about what a tool does. Kept small and explicit
# rather than pulled from a stopword corpus, so the similarity number stays
# reproducible without a dependency.
STOPWORDS = frozenset(
    "the a an and or of to in for by on with is are be as at from that this it its use uses "
    "using when if you your not no can will".split()
)

# A description that says only what a tool does, never when to reach for it.
# Prescriptive triggers measurably lift the rate at which a model calls the
# right tool, so this is worth flagging — and unlike the two checks cut above it
# discriminates, firing on half the catalogue rather than all of it.
TRIGGER_PHRASE = re.compile(r"\b(use (this|it|when|for)|call (this|it|when)|when you|if you|for when|use to)\b", re.I)

# Measured distribution of description length across the 30-tool catalogue:
# min 143, p25 302, p50 445, max 713 characters. 200 sits below p25 and above
# the floor, so it flags the genuinely terse without indicting a quarter of the
# catalogue for being merely concise.
MIN_DESCRIPTION_CHARS = 200

# Rough and deliberately labelled as such: ~4 characters per token is close
# enough for a *relative* comparison between tools, which is all this is for.
# An exact count needs a tokenizer per provider, and would change nothing about
# which tool is the biggest.
CHARS_PER_TOKEN = 4


def tokens(text: str | None) -> set[str]:
    """Content words of a description, lowercased and de-duplicated.

    `None` is a real catalogue value: an app-manifest tool ships without a
    description, which is what the `no_description` finding is for."""
    words: list[str] = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def similarity(left: str | None, right: str | None) -> float:
    """Jaccard overlap of two descriptions' content words, 0.0 to 1.0.

    See the module docstring for why this is an explainer and not a predictor.
    """
    a, b = tokens(left), tokens(right)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _estimate_tokens(value: object) -> int:
    return len(json.dumps(value)) // CHARS_PER_TOKEN if value else 0


def lint_tool(tool: ToolDef) -> list[str]:
    """Finding codes for one tool, empty when it is clean."""
    findings: list[str] = []
    description = tool.get("description") or ""
    if not description:
        findings.append("no_description")
    elif len(description) < MIN_DESCRIPTION_CHARS:
        findings.append("short_description")
    if description and not TRIGGER_PHRASE.search(description):
        findings.append("no_trigger_phrase")
    return findings


def catalogue_facts(tools: list[ToolDef]) -> CatalogueFacts:
    """The uniform properties — counted once, because per-tool they are noise."""
    params = undocumented = strings = unconstrained = 0
    for tool in tools:
        properties = as_object(as_object(tool.get("inputSchema")).get("properties"))
        for key in properties:
            spec = as_object(properties.get(key))
            if not spec:
                continue
            params += 1
            if not spec.get("description"):
                undocumented += 1
            if spec.get("type") == "string":
                strings += 1
                if not (spec.get("enum") or spec.get("format") or spec.get("pattern")):
                    unconstrained += 1
    return CatalogueFacts(
        tools=len(tools),
        params=params,
        params_undocumented=undocumented,
        string_params=strings,
        string_params_unconstrained=unconstrained,
        description_tokens=sum(_estimate_tokens(t.get("description")) for t in tools),
        schema_tokens=sum(_estimate_tokens(t.get("inputSchema")) for t in tools),
    )


def lint(snapshot: Snapshot) -> LintReport:
    tools = snapshot.get("tools", [])
    by_tool: dict[str, ToolLintEntry] = {}
    for tool in sorted(tools, key=lambda t: t.get("name", "")):
        name = tool.get("name")
        if not name:
            continue
        by_tool[name] = ToolLintEntry(
            findings=lint_tool(tool),
            description_chars=len(tool.get("description") or ""),
            schema_tokens=_estimate_tokens(tool.get("inputSchema")),
        )
    return LintReport(facts=catalogue_facts(tools), tools=by_tool, similar_pairs=similar_pairs(tools))


def similar_pairs(tools: list[ToolDef], limit: int = 10) -> list[SimilarPair]:
    """Most textually similar description pairs, worst-first.

    Advisory only — see the module docstring. Rendered with its measured hit
    rate attached so nobody reads the top entry as a defect.
    """
    scored: list[SimilarPair] = []
    for left, right in combinations(sorted(tools, key=lambda t: t.get("name", "")), 2):
        if not (left.get("name") and right.get("name")):
            continue
        score = similarity(left.get("description") or "", right.get("description") or "")
        if score:
            scored.append(SimilarPair(pair=(left["name"], right["name"]), similarity=round(score, 3)))
    return sorted(scored, key=lambda s: -s["similarity"])[:limit]


BUDGETED = ("params_undocumented", "string_params_unconstrained")

# Next to the snapshot it bounds, and read by both this module's CLI and
# eval/summary.py — which renders the same lint inside the eval job summary.
# One constant so the two cannot end up reporting against different ceilings.
DEFAULT_BUDGET = "tool-history/lint-budget.json"


def load_budget(path: str | Path) -> LintBudget | None:
    """The committed ceiling, or None when there isn't a usable one.

    None rather than a raise: an absent budget means the counts are not
    ratcheted, which is a thing to report loudly, not to crash over.
    """
    try:
        return cast(LintBudget, json.loads(Path(path).read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def budget_from(facts: CatalogueFacts) -> LintBudget:
    """The budget a snapshot would set if it were the new ceiling."""
    return LintBudget(
        params_undocumented=facts["params_undocumented"],
        string_params_unconstrained=facts["string_params_unconstrained"],
    )


def budget_breaches(facts: CatalogueFacts, budget: LintBudget) -> list[str]:
    """Counts that rose above their ceiling, worst first, empty when clean.

    Returns the rendered message rather than the key so the caller does not
    have to re-derive the numbers to say what happened; there are two of these,
    and a structured result nobody destructures is just a longer string.
    """
    # Biggest overage first, ties broken by BUDGETED order rather than by the
    # key's spelling — both counts move together when an undescribed string
    # parameter lands, and "+1 and +1" sorted alphabetically would lead with
    # the narrower of the two for no reason a reader could infer.
    breaches = sorted(
        ((facts[key] - budget[key], rank, key) for rank, key in enumerate(BUDGETED) if facts[key] > budget[key]),
        key=lambda b: (-b[0], b[1]),
    )
    return [
        f"`{key}` rose to {facts[key]}, above the committed ceiling of {budget[key]} "
        f"(+{over}). Document the new parameter, or lower the ceiling deliberately "
        f"with `--update-budget` and say why in the commit."
        for over, _rank, key in breaches
    ]


def render(report: LintReport, budget: LintBudget | None = None) -> str:
    facts = report["facts"]
    flagged = {n: t for n, t in report["tools"].items() if t["findings"]}

    lines = [
        "## Tool catalogue lint",
        "",
        f"{facts['tools']} tools · descriptions ≈{facts['description_tokens']} tokens · "
        f"schemas ≈{facts['schema_tokens']} tokens "
        f"(the whole catalogue costs ≈{facts['description_tokens'] + facts['schema_tokens']} tokens to advertise)",
        "",
    ]

    if flagged:
        lines += ["| Tool | Finding | Description chars |", "|---|---|---:|"]
        for name, entry in sorted(flagged.items()):
            lines.append(f"| `{name}` | {', '.join(entry['findings'])} | {entry['description_chars']} |")
        lines.append("")
    else:
        lines += ["No per-tool findings.", ""]

    def ceiling(key: str) -> str:
        return "" if budget is None else f" (ceiling {budget[key]})"

    lines += [
        "**Catalogue-wide.** Reported once because they are uniform, not per tool:",
        "",
        f"- {facts['params_undocumented']}/{facts['params']} parameters carry no schema-level "
        f"`description`{ceiling('params_undocumented')}; this server documents parameters in prose "
        "inside the tool description.",
        f"- {facts['string_params_unconstrained']}/{facts['string_params']} string parameters have no "
        f"`enum`, `format` or `pattern`{ceiling('string_params_unconstrained')}.",
        "",
    ]

    if budget is None:
        lines += [
            "> No committed budget, so neither count is ratcheted. Create one with "
            "`python -m toollint --update-budget`.",
            "",
        ]
    else:
        breaches = budget_breaches(facts, budget)
        for message in breaches:
            lines += [f"❌ {message}", ""]
        if not breaches:
            lines += ["Both counts are at or below their committed ceiling.", ""]

    if report["similar_pairs"]:
        lines += [
            "**Most similar descriptions** — advisory, and a weak signal: measured against",
            "confirmed confusions it ranks most of them in the top 15% of all pairs, but the",
            "top of this list is dominated by pairs that have never actually been confused.",
            "Use it to explain a confirmed collision, not to predict one.",
            "",
        ]
        for entry in report["similar_pairs"]:
            lines.append(f"- {entry['similarity']:.3f} — `{entry['pair'][0]}` / `{entry['pair'][1]}`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", default="tool-history/latest.json", help="Catalogue snapshot to lint")
    parser.add_argument(
        "--budget",
        default=DEFAULT_BUDGET,
        help="Committed ceiling for the budgeted parameter counts (default tool-history/lint-budget.json)",
    )
    parser.add_argument(
        "--update-budget",
        action="store_true",
        help="Re-stamp the budget from this snapshot instead of checking against it",
    )
    args = parser.parse_args()

    snapshot_path = cast(str, args.snapshot)
    budget_path = Path(cast(str, args.budget))
    try:
        snapshot = cast(Snapshot, json.loads(Path(snapshot_path).read_text()))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not read {snapshot_path}: {exc}", file=sys.stderr)
        return 1

    report = lint(snapshot)

    if cast(bool, args.update_budget):
        budget = budget_from(report["facts"])
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        budget_path.write_text(json.dumps(budget, indent=2) + "\n")
        print(f"Wrote {budget_path}: {json.dumps(budget)}")
        return 0

    # A missing budget warns rather than fails. It is the same call the drift
    # step makes for a missing snapshot baseline, and for the same reason: a
    # lint that goes red because a data file is absent — on a workflow that
    # runs on every push — is one people learn to bypass. The warning is loud
    # in the job summary, which is where an unratcheted count belongs.
    budget = load_budget(budget_path)
    if budget is None:
        print(f"::warning::No usable lint budget at {budget_path}; counts are not ratcheted.", file=sys.stderr)

    print(render(report, budget))

    # The prose findings stay advisory: they are style judgements about word
    # choice, and a build that goes red over those is one people learn to
    # bypass. The two budgeted counts are not word choice — they are a
    # parameter contract the eval has already caught the absence of — so they
    # are the only thing here that gates.
    if budget is not None and budget_breaches(report["facts"], budget):
        for message in budget_breaches(report["facts"], budget):
            print(f"::error::toollint: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
