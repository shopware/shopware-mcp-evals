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


def tokens(text: str) -> set[str]:
    """Content words of a description, lowercased and de-duplicated."""
    words: list[str] = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def similarity(left: str, right: str) -> float:
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


def render(report: LintReport) -> str:
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

    lines += [
        "**Catalogue-wide.** Reported once because they are uniform, not per tool:",
        "",
        f"- {facts['params_undocumented']}/{facts['params']} parameters carry no schema-level "
        "`description`; this server documents parameters in prose inside the tool description.",
        f"- {facts['string_params_unconstrained']}/{facts['string_params']} string parameters have no "
        "`enum`, `format` or `pattern`.",
        "",
    ]

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
    args = parser.parse_args()

    snapshot_path = cast(str, args.snapshot)
    try:
        snapshot = cast(Snapshot, json.loads(Path(snapshot_path).read_text()))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not read {snapshot_path}: {exc}", file=sys.stderr)
        return 1

    markdown = render(lint(snapshot))
    print(markdown)
    # Advisory: this reports, it does not gate. The findings are style
    # judgements about prose, and a build that goes red over word choice is one
    # people learn to bypass.
    return 0


if __name__ == "__main__":
    sys.exit(main())
