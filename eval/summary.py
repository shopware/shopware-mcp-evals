#!/usr/bin/env python3
"""Render the GitHub Actions job summary for a whole eval run.

The workflow runs `eval/runner.py` three times — admin primary, admin second
validator, Store/UCP advisory — in three separate processes, with the
cross-model comparison step in between. Each one used to append its own
markdown table to GITHUB_STEP_SUMMARY, which produced:

  * three one-row tables instead of one three-row table, each re-printing the
    same header, because markdown appended from separate processes cannot be
    joined into a single table;
  * the primary and second-validator rates printed twice — once in their own
    table, again in the cross-model table underneath;
  * an unlabelled third row (`openai` / `gpt-4o`, 42 fixtures) that nothing
    identified as the Store suite.

So the runs now emit their verdicts as JSON rows (`run.py --summary-row`) and
this module renders everything once, at the end of the job. Ordering comes
from the row filenames, which the workflow numbers.

Usage:
    python -m eval.summary --rows results/rows --comparison results/eval-comparison.json
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path

import toollint
from eval.compare_runs import render_actionable, render_detail, render_split, render_unmatched
from eval.cost import combine
from eval.tool_scorecard import collisions, rank_worst, scorecard
from ownership import TIER_ORDER

# Prefix for synthetic fixture ids, used only when a summary row predates the
# `ids` field. Chosen because a real fixture id is a YAML identifier and can
# never start with it, so `startswith` is a safe test for "not a real fixture".
ANON = "?"


def details(label: str, body: str) -> str:
    """A collapsed block, for reference tables rather than verdicts.

    The summary is read to answer "did it pass, and what do I fix". Measured on
    one run, the verdict was 738 of 9,470 bytes and the per-tool scorecard alone
    was 3,750 — so the page opened on reference data and the answer was four
    scrolls down. Everything still ships; it just does not compete for the top.

    The blank line after </summary> is load-bearing: without it GitHub renders
    the markdown inside as literal text.
    """
    if not body.strip():
        return ""
    return f"<details>\n<summary>{label}</summary>\n\n{body.strip()}\n\n</details>\n"


def nest(label: str, body: str) -> str:
    """Collapse a section that already renders its own `###` heading.

    The heading is dropped rather than kept: a `<summary>` label with the same
    words immediately under it reads as a rendering bug.
    """
    if not body.strip():
        return ""
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return details(label, "\n".join(lines))


def para(*sentences: str) -> str:
    """One paragraph as ONE line.

    GitHub renders a single newline in a step summary as a line break, so prose
    hard-wrapped in the source arrived ragged — wrapped at the source's ~70
    columns inside a browser column three times that wide. Passing the sentences
    separately keeps this file readable without leaking its line endings into the
    output.
    """
    return " ".join(s.strip() for s in sentences if s.strip())


def load_rows(rows_dir: Path) -> list[dict]:
    """Read the per-run rows, ordered by filename.

    A missing directory or an unreadable row is not fatal: the summary is a
    reporting convenience and must never be the thing that turns a run red.
    An absent row is reported by its absence — a run that did not happen has
    no line in the table, which is the honest rendering.
    """
    if not rows_dir.is_dir():
        return []
    rows = []
    for path in sorted(rows_dir.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::warning::Could not read summary row {path}: {exc}", file=sys.stderr)
    return rows


def render_runs(rows: list[dict]) -> str:
    """One table for every eval run in the job, suite-labelled."""
    if not rows:
        return "No eval run reported a result.\n"

    lines = [
        "| Suite | Provider | Model | Pass rate | Graded | Errors | Throttled | Cost | Gate |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        gate = r.get("gate", "?")
        # An advisory suite is continue-on-error, so a FAIL there is a finding
        # to read, not a broken build. Saying so in the cell stops it being
        # mistaken for the gating verdict.
        if r.get("advisory"):
            gate = f"{gate} (advisory)"
        lines.append(
            f"| {r.get('suite', '?')} | `{r.get('provider', '?')}` | `{r.get('model', '?')}` | "
            f"{round(100 * r.get('rate', 0))}% | {r.get('graded', 0)} | {r.get('errored', 0)} | "
            f"{r.get('throttled', 0)} | {_usd(r.get('cost'))} | {gate} |"
        )
    lines.append("")
    return "\n".join(lines)


def _usd(cost: dict | None) -> str:
    """A run's dollar cost, or an explicit gap.

    "—" for a run with no cost block at all (an older row) and "unpriced" for
    one whose model is missing from pricing.yaml. Neither renders as $0.00,
    which would read as free rather than as unknown.
    """
    if not cost:
        return "—"
    if cost.get("total_usd") is None:
        return "unpriced"
    return f"${cost['total_usd']:.2f}" + ("*" if cost.get("unverified") else "")


def _tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def render_cost(rows: list[dict]) -> str:
    """The job's total cost, and the two numbers that make it actionable.

    Cost per fixture says what a data point costs. Cost per *passing* fixture
    says what a unit of signal costs, which is the one that survives a change to
    the suite: a run that costs more but converts failures into passes got
    cheaper by this measure and more expensive by the total.
    """
    costs = [r["cost"] for r in rows if r.get("cost")]
    if not costs:
        return ""

    job = combine(costs)
    graded = sum(r.get("graded", 0) for r in rows)
    passed = sum(c.get("passed", 0) for c in costs)
    tokens = job["tokens"]

    total = f"${job['total_usd']:.2f}"
    if not job["complete"]:
        total += f" (incomplete — no price for {', '.join(m for m in job['unpriced_models'] if m)})"

    parts = [
        f"**Cost of this run:** {total} · "
        f"{_tokens(tokens['input'])} input ({_tokens(tokens['cached_input'])} cached) · "
        f"{_tokens(tokens['output'])} output"
    ]
    if graded and job["total_usd"]:
        per_fixture = job["total_usd"] / graded
        parts.append(f"${per_fixture:.4f} per fixture")
    if passed and job["total_usd"]:
        parts.append(f"${job['total_usd'] / passed:.4f} per *passing* fixture")

    lines = [" · ".join(parts), ""]
    verified = next((c.get("verified") for c in costs if c.get("verified")), None)
    notes = [f"Prices last verified {verified}." if verified else None]
    if job["unverified_models"]:
        notes.append(f"\\* estimated rates for {', '.join(job['unverified_models'])} — see `pricing.yaml`.")
    notes = [n for n in notes if n]
    if notes:
        lines += [f"<sub>{' '.join(notes)}</sub>", ""]
    return "\n".join(lines)


# What each combination of arm outcomes points at. Keyed by
# (passed_isolated, passed_full); the discovery arm failed in every row, which
# is the only reason the fixture is here.
DIAGNOSIS = {
    (False, False): (
        "the tool's own description",
        "It loses even against its own group. Rewrite the description, or the fixture is wrong.",
    ),
    (True, False): (
        "a cross-group collision",
        "Distinguishable among its siblings, beaten by something in another group — fix the pair, not the tool.",
    ),
    (True, True): (
        "the discovery layer",
        "It wins whenever it is advertised, so the problem is being found: the "
        "`#[McpToolGroup]` description, or tool-search ranking.",
    ),
    (False, True): (
        "intra-group ambiguity",
        "Beaten by a sibling but not by the wider catalogue — unusual; check the "
        "group's descriptions against each other.",
    ),
}


def render_arm_matrix(reports: list[dict]) -> str:
    """Where each discovery failure actually lives.

    The discovery arm says a fixture failed. It cannot say why, because three
    different problems produce the same symptom: a bad description, a collision
    with a tool in another group, or a discovery layer that never surfaced the
    right tool at all. Re-running just the failures with the group pre-enabled,
    and then with the whole catalogue enabled, separates them.
    """
    arms = {}
    for report in reports:
        for arm in ("discovery", "isolated", "full"):
            mode = (report.get("modes") or {}).get(arm)
            if mode:
                arms.setdefault(arm, {}).update({r["id"]: r for r in mode.get("results", [])})

    if not (arms.get("isolated") or arms.get("full")):
        return ""

    triaged = sorted(set(arms.get("isolated", {})) | set(arms.get("full", {})))
    lines = [
        "### Where the failures are",
        "",
        para(
            "Each fixture below failed the gating discovery arm, then was re-run with only its own",
            "toolset enabled, and again with the whole catalogue enabled. The combination says which",
            "of three different problems produced the same symptom.",
        ),
        "",
        "| Fixture | Expected | isolated | full | Diagnosis |",
        "|---|---|:---:|:---:|---|",
    ]
    buckets = {}
    unusable = []
    for fid in triaged:
        iso = arms.get("isolated", {}).get(fid)
        full = arms.get("full", {}).get(fid)
        if iso is None or full is None:
            continue
        # An arm that could not put the tool in front of the model answers
        # nothing, and must not be read as a verdict on the description. This
        # is the whole reason the check exists: the first Store triage produced
        # an empty surface for every fixture, and diagnosing that as "the tool's
        # own description" was confidently wrong sixteen times over.
        if iso.get("skipped") or full.get("skipped"):
            unusable.append((fid, (iso if iso.get("skipped") else full).get("skip_reason", "")))
            continue
        key = (bool(iso.get("passed")), bool(full.get("passed")))
        label, _ = DIAGNOSIS[key]
        buckets.setdefault(label, []).append(fid)
        lines.append(
            f"| `{fid}` | `{iso.get('expected_tool') or full.get('expected_tool')}` | "
            f"{'✓' if key[0] else '✗'} | {'✓' if key[1] else '✗'} | {label} |"
        )
    lines.append("")

    for label, explanation in DIAGNOSIS.values():
        if label in buckets:
            lines.append(f"- **{label}** ({len(buckets[label])}) — {explanation}")
    if unusable:
        lines += [
            "",
            f"**{len(unusable)} could not be diagnosed.** The arm never advertised the tool, so it",
            "answers nothing about the description — the setup failed, not the fixture:",
            "",
        ]
        for fid, reason in unusable:
            lines.append(f"- `{fid}` — {reason}")

    diagnosed = len(triaged) - len(unusable)
    lines.append("")
    lines.append(
        f"<sub>Triaged {len(triaged)} discovery failures, {diagnosed} diagnosed. Passing fixtures are not re-run.</sub>"
    )
    lines.append("")
    return "\n".join(lines)


def render_tiers(rows: list[dict]) -> str:
    """Pass rates per owning repository, across every suite in the job.

    The run table above says "admin · primary 92%", but admin spans core,
    the dev-tools bundle and the merchant-tools plugin. Those failures are not
    worth the same: core ships to every merchant, the plugins are optional.
    This is the table that says which one moved.
    """
    # Counted per fixture, not per fixture-and-run. The admin primary and the
    # second validator grade the same 90 admin fixtures, so summing `total`
    # across rows doubled every admin denominator — dev-tools read 36/42 for 21
    # fixtures — and a fixture both models missed was listed twice in the
    # failing cell. Worse, that weighting is backwards: a both-model failure
    # counted twice against the rate while a single-model one counted once,
    # inflating exactly the capability-gap misses the cross-model table exists
    # to discount.
    graded: dict[str, collections.Counter] = {}
    failed: dict[str, collections.Counter] = {}
    advisory: dict[str, bool] = {}
    for index, r in enumerate(rows):
        for tier, b in (r.get("by_tier") or {}).items():
            ids = b.get("ids")
            fails = b.get("failed_ids") or []
            if ids is None:
                # A row written before `ids` existed, so nothing can dedupe: fall
                # back to the old summed behaviour by giving every slot a
                # row-unique placeholder. `passed`/`total` drive the split rather
                # than `failed_ids`, which older rows could under-populate — using
                # the id list alone would report such a row as entirely clean.
                total, fails = b.get("total", 0), list(fails)
                anonymous = max(0, (total - b.get("passed", 0)) - len(fails))
                fails += [f"{ANON}fail·{tier}#{index}#{n}" for n in range(anonymous)]
                ids = fails + [f"{ANON}pass·{tier}#{index}#{n}" for n in range(max(0, total - len(fails)))]
            graded.setdefault(tier, collections.Counter()).update(ids)
            failed.setdefault(tier, collections.Counter()).update(fails)
            # Advisory only if every suite that contributed to this tier was.
            advisory[tier] = advisory.get(tier, True) and bool(r.get("advisory"))
    if not graded:
        return ""

    ordered = [t for t in TIER_ORDER if t in graded] + [t for t in graded if t not in TIER_ORDER]
    lines = [
        "### By owner",
        "",
        para(
            "Failures are not worth the same: core ships to every merchant, the plugins are optional.",
            "Core is held to its own denominator so a regression there cannot be averaged away by",
            "clean plugin numbers.",
        ),
        "",
        para(
            "Counted per fixture across every suite, so a fixture graded by both admin runs counts",
            "once rather than twice. **Clean** is fixtures that passed on every model that graded",
            "them, which is why this rate is stricter than the per-suite rates above — one model",
            "missing once is enough to leave a fixture out of it.",
        ),
        "",
        para(
            "**Bold** in the last column marks a fixture that at least two models graded and *all* of them",
            "failed — the actionable set, matching the both-fail row of the cross-model table below. Plain",
            "means either some model passed it (usually the weaker one's capability gap) or only one model",
            "graded it at all, and a single run is not evidence about a description.",
        ),
        "",
        "| Owner | Clean | Rate | Failed on every model | Enforcement | Failing fixtures |",
        "|---|---:|---:|---:|---|---|",
    ]
    for tier in ordered:
        runs_by_id, fails_by_id = graded[tier], failed[tier]
        total = len(runs_by_id)
        # A fixture counts as failing the owner if any run missed it; the
        # failed-everywhere subset is the one worth acting on.
        #
        # The two-run floor is the point. The Store suite runs a single model, so
        # without it every store failure satisfied "failed on every model that
        # graded it" and got bolded as actionable — two of them were, while the
        # cross-model table right below reported one. That is precisely the
        # single-run noise the two-model split exists to discount, presented with
        # the weight of a confirmed finding.
        #
        # Placeholders from a legacy row are excluded too: they count towards the
        # denominator but have no id to correlate across runs.
        failing_ids = sorted(fails_by_id)
        consistent = {
            i
            for i in failing_ids
            if not i.startswith(ANON) and runs_by_id.get(i, 0) >= 2 and fails_by_id[i] >= runs_by_id[i]
        }
        passed = total - len(failing_ids)
        pct = round(100 * passed / total) if total else 0
        lines.append(
            f"| {tier} | {passed}/{total} | {pct}% | {len(consistent) or '—'} | "
            f"{_enforcement(tier, advisory[tier])} | {_failing(failing_ids, consistent)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _enforcement(tier: str, advisory: bool) -> str:
    """What actually fails the build for this tier — not what we wish did.

    Three distinct states, and conflating them is how "optional" gets read as
    "unenforced": an optional plugin's fixtures still count towards its suite's
    aggregate rate, unless the whole suite is continue-on-error.
    """
    if advisory:
        return "advisory"
    if tier.startswith("core"):
        return "**core gate** + suite rate"
    return "suite rate"


def _failing(ids: list, consistent: set | None = None) -> str:
    """Failing fixture ids, the all-models ones bolded.

    Truncation puts the bolded ids first: with a cap of six, an owner with eight
    failures could otherwise hide its only actionable one behind five
    capability-gap misses.

    Placeholder ids from a legacy row have no fixture name to print, so they are
    reported as a count rather than rendered — printing `?fail·core#0#2` would
    read as a fixture that does not exist.
    """
    named = sorted(i for i in ids if not i.startswith(ANON))
    anonymous = len(ids) - len(named)
    if not named:
        return f"{anonymous} unnamed (older run)" if anonymous else "—"

    consistent = consistent or set()
    ordered = sorted(named, key=lambda i: (i not in consistent, i))
    shown = ", ".join(f"**`{i}`**" if i in consistent else f"`{i}`" for i in ordered[:6])
    extra = []
    if len(ordered) > 6:
        extra.append(f"+{len(ordered) - 6} more")
    if anonymous:
        extra.append(f"+{anonymous} unnamed")
    return shown + (f" ({', '.join(extra)})" if extra else "")


def render_comparison(cmp_: dict | None) -> str:
    """The cross-model section, minus the per-model rates.

    Those rates are already in the run table above — repeating them is exactly
    the redundancy this module exists to remove. What is kept is the part the
    run table cannot show: which fixtures both models missed, and what each of
    them picked instead.
    """
    if cmp_ is None:
        return (
            "### Cross-model comparison\n\n"
            "Not available — the comparison step did not produce a report "
            "(one of the two eval runs did not finish).\n"
        )

    primary = cmp_.get("primary", {}).get("model", "primary")
    second = cmp_.get("second", {}).get("model", "second")
    return "\n".join(
        [
            "### Cross-model comparison (discovery mode)",
            "",
            para(
                "A fixture both models miss points at the tool description. One only the weaker model",
                "misses is its capability gap; one only the stronger misses is usually flaky discovery.",
            ),
            "",
            render_split(cmp_),
            render_actionable(cmp_, primary, second),
            render_detail(cmp_, primary, second),
            render_unmatched(cmp_),
        ]
    )


def load_reports(paths: list[str] | None) -> list[dict]:
    """Read the full per-run reports named by the caller.

    The rows carry a verdict; the scorecard needs every fixture's selection.

    Explicit paths, deliberately not a directory glob. `results/` is committed
    and holds every historical report, so globbing `eval-*.json` there would
    pool runs from months ago — against older descriptions, older fixtures and
    in one case a mode that no longer exists — into a table presented as this
    job's. It would look right and be wrong. The workflow knows exactly which
    three files it just wrote, so it names them.

    A report that is simply absent is silent: the Store suite is conditional, so
    warning about it would fire on most runs, and a warning that always fires is
    one nobody reads — which is how the corrupt-file case below would get
    missed. A report that exists but cannot be parsed is a real problem and says
    so. Either way it costs a table, never the build.
    """
    reports = []
    for path in paths or []:
        try:
            reports.append(json.loads(Path(path).read_text()))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            print(f"::warning::Could not read eval report {path}: {exc}", file=sys.stderr)
    return reports


def load_snapshot(path: str | None) -> dict | None:
    """A whole catalogue snapshot, or None.

    None rather than {} so a caller can tell "no snapshot for this endpoint" from
    "a snapshot with nothing in it" — the Store one is absent on any run whose
    static job did not reach it, and that omits a section rather than rendering
    an empty one.
    """
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::Could not read tool catalogue {path}: {exc}", file=sys.stderr)
        return None


def load_catalog(path: str | None) -> dict[str, dict]:
    """Tool name -> full definition, from a snapshot written by snapshot_tools.py.

    compare_runs.load_catalogue reads the same file but keeps only descriptions;
    the scorecard wants the whole definition so it can report on the schema too.
    """
    snapshot = load_snapshot(path)
    if not snapshot:
        return {}
    return {t["name"]: t for t in snapshot.get("tools", []) if t.get("name")}


def pooled_results(reports: list[dict]) -> list[dict]:
    """Every graded discovery result in the job, across suites and models.

    Pooled rather than deduplicated per fixture, which is the opposite of what
    render_tiers does. The tier table answers "did this fixture pass", so
    counting it once per model would double-weight the admin suite. The
    scorecard answers "how good is this description", and a description that
    only misleads the weaker model is still a weaker description — every graded
    observation is evidence, so every one counts.

    The `discovery` arm only. The triage arms re-run the same failures under a
    different advertised surface, which is a different experiment: folding them
    in counted one failure three times and inflated every confusion pair by
    however many arms happened to run. They have their own table.
    """
    return [
        r
        for report in reports
        for arm, mode in report.get("modes", {}).items()
        if arm == "discovery"
        for r in mode.get("results", [])
    ]


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{round(100 * value)}%"


def _partners(counts: dict[str, int], limit: int = 2) -> str:
    shown = [f"`{name}` ×{n}" for name, n in list(counts.items())[:limit]]
    if len(counts) > limit:
        shown.append(f"+{len(counts) - limit}")
    return ", ".join(shown) or "–"


def render_tool_scorecard(results: list[dict], catalog: dict[str, dict]) -> str:
    """Per-tool recall, precision and confusion — the half recall alone can't show.

    Recall is what the pass rate already reports: did this tool win the fixtures
    written for it. Precision is new, and it is the number that catches an
    over-broad description — a tool that wins its own fixtures *and* its
    siblings' scores 100% on recall while actively making the catalogue worse.
    Sorted worst-F1 first so the tool to fix is the first row.
    """
    if not results:
        return ""

    card = scorecard(results, catalog)
    if not card:
        return ""

    ranked = rank_worst(card)
    flagged = [(n, e) for n, e in ranked if _needs_work(e)]

    lines = [
        "### Per-tool scorecard",
        "",
        para(
            "Recall = won the fixtures written for it. Precision = was right when picked. A tool with",
            "high recall and low precision has an over-broad description: it is winning its siblings'",
            "prompts, which the pass rate alone cannot show.",
        ),
        "",
    ]
    if flagged:
        lines += [
            para(
                f"**{len(flagged)} of {len(ranked)} tools below {ACTIONABLE_F1:.0%} on recall or precision.**",
                "The rest are clean and are in the full table below.",
            ),
            "",
            _scorecard_table(flagged),
        ]
    else:
        lines += [para(f"All {len(ranked)} tools are at or above {ACTIONABLE_F1:.0%} on both."), ""]

    # The full table stays, collapsed: it is the reference anyone comparing two
    # runs by hand needs, and it was 40% of the summary when it led the section.
    lines.append(details(f"Full scorecard — all {len(ranked)} tools", _scorecard_table(ranked)))

    found = collisions(card)
    if found:
        pairs = [
            f"- `{p['pair'][0]}` / `{p['pair'][1]}` — {p['total']} miss(es)" + (" **(mutual)**" if p["mutual"] else "")
            for p in found
        ]
        lines += [
            para(
                "**Confusion pairs.** A mutual pair is the strongest signal here — both descriptions",
                "attract each other's prompts, so they need differentiating from each other rather than",
                "fixing one at a time.",
            ),
            "",
            # `collisions` returns them worst-first, so the tail is the long thin
            # end of one-off misses. Thirteen bullets of it pushed the cross-model
            # verdict off the first screen.
            *pairs[:TOP_COLLISIONS],
            "",
        ]
        if len(pairs) > TOP_COLLISIONS:
            lines.append(
                details(f"The remaining {len(pairs) - TOP_COLLISIONS} pairs", "\n".join(pairs[TOP_COLLISIONS:]))
            )
    return "\n".join(lines)


# How many confusion pairs stay in front of the reader.
TOP_COLLISIONS = 5


# Below this on recall or precision and the tool is worth someone's attention.
# Not a gate — nothing here fails a build — just the line between "read this row"
# and "it is in the appendix".
ACTIONABLE_F1 = 0.9


def _needs_work(entry: dict) -> bool:
    """Whether a tool's row belongs in front of the reader.

    Either half can be the problem and they mean opposite things: low recall is a
    description nobody finds, low precision is one that wins prompts it has no
    business winning. A tool with no fixtures at all is flagged too — an
    uncovered tool is the one failure mode the rates cannot show.
    """
    if not entry.get("expected_n"):
        return True
    return any(entry.get(k) is not None and entry[k] < ACTIONABLE_F1 for k in ("recall", "precision"))


def _scorecard_table(entries: list[tuple[str, dict]]) -> str:
    lines = [
        "| Tool | Fixtures | Recall | Picked | Precision | F1 | Steals from | Search rank |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for name, e in entries:
        rank = "–" if e["search_rank_p50"] is None else f"{e['search_rank_p50']:g}"
        lines.append(
            f"| `{name}` | {e['expected_n']} | {_pct(e['recall'])} | {e['selected_n']} | "
            f"{_pct(e['precision'])} | {_pct(e['f1'])} | {_partners(e['steals_from'])} | {rank} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_catalogue(snapshot: dict | None, label: str) -> str:
    """One endpoint's toolsets and their tools.

    Both endpoints or neither. The Store listing used to be written straight to
    the summary from the static job with no admin equivalent, so the page
    documented the 17 tools of the optional plugin and said nothing about the 30
    that ship in core.
    """
    if not snapshot:
        return ""
    toolsets = snapshot.get("toolsets") or []
    tools = snapshot.get("tools") or []
    if not toolsets and not tools:
        return ""
    lines = [f"{len(tools)} tools in {len(toolsets)} toolsets", ""]
    for ts in toolsets:
        names = ", ".join(f"`{t}`" for t in ts.get("tools", []))
        lines.append(f"- **{ts.get('name', '?')}** — {names}")
    return details(f"{label} catalogue — {len(tools)} tools", "\n".join(lines))


def render_catalogue_lint(snapshot: dict | None) -> str:
    """The static description findings, rendered here rather than in a second
    workflow's summary.

    toollint is pure — it reads a committed snapshot, needs no server and no
    model — so running it again in this job costs nothing and is what lets one
    page carry the whole picture. It stays a gate-free advisory: the findings are
    judgements about prose, and a build that goes red over word choice is one
    people learn to bypass.
    """
    if not snapshot:
        return ""
    body = toollint.render(toollint.lint(snapshot))
    # Its own H2 would compete with this page's; the section is nested here.
    body = body.replace("## Tool catalogue lint\n", "").strip()
    return details("Tool catalogue lint — static description findings", body)


def render(
    rows: list[dict],
    cmp_: dict | None,
    results: list[dict] | None = None,
    catalog: dict | None = None,
    reports: list[dict] | None = None,
    admin_snapshot: dict | None = None,
    store_snapshot: dict | None = None,
) -> str:
    """The whole job on one page, verdict first.

    Ordered by what a reader came for. Above the fold: did it pass, what did it
    cost, and which fixtures or tools to act on. Below, collapsed: the reference
    tables and the static catalogue material, which are what you open when the
    answer above raises a question.

    This is one page on purpose. The same run used to write five separate
    summaries from four jobs across two workflows — the catalogue lint in the
    lint workflow, a Store-only catalogue listing in the static job, a
    comparison note in the eval job — so whether you saw a finding depended on
    which job you happened to click.
    """
    return "\n".join(
        [
            "## MCP evals",
            "",
            render_runs(rows),
            render_cost(rows),
            # Directly under the rates, deliberately: the denominator they are
            # computed over belongs next to them, not in an appendix.
            render_skipped(reports or []),
            render_tiers(rows),
            render_tool_scorecard(results or [], catalog or {}),
            render_comparison(cmp_),
            # Reference from here down.
            nest("Context prompt — what the tool guide is worth", render_prompt_delta(reports or [])),
            nest("Triage — which arm located each failure", render_arm_matrix(reports or [])),
            render_catalogue_lint(admin_snapshot),
            render_catalogue(admin_snapshot, "Admin"),
            render_catalogue(store_snapshot, "Store"),
        ]
    )


# GitHub silently discards a step summary over this size, leaving the step green
# and the run page blank. 1 MiB, per the Actions docs.
GITHUB_SUMMARY_LIMIT = 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--rows", default="results/rows", help="Directory of per-run summary rows (default results/rows)"
    )
    parser.add_argument("--comparison", help="Path to the cross-model comparison JSON (eval/compare_runs.py --output)")
    parser.add_argument(
        "--reports",
        nargs="*",
        help=(
            "Full eval reports written by THIS job, named explicitly, for the per-tool "
            "scorecard. Not a glob: results/ is committed and holds historical reports "
            "that must not be pooled into this job's table."
        ),
    )
    parser.add_argument(
        "--tools",
        default="tool-history/latest.json",
        help="Tool catalogue snapshot, so the scorecard can flag tools no fixture covers",
    )
    parser.add_argument(
        "--store-tools",
        default="tool-history/store.json",
        help=(
            "Store catalogue snapshot, for the Store listing. Absent on any run whose "
            "static job did not snapshot it, which simply omits that section."
        ),
    )
    args = parser.parse_args()

    cmp_ = None
    if args.comparison:
        try:
            cmp_ = json.loads(Path(args.comparison).read_text())
        except (OSError, json.JSONDecodeError):
            # Expected whenever the comparison step skipped, which it does when
            # either eval run failed to produce a report. Rendered as a note in
            # the summary rather than warned about here.
            cmp_ = None

    reports = load_reports(args.reports)
    markdown = render(
        load_rows(Path(args.rows)),
        cmp_,
        pooled_results(reports),
        load_catalog(args.tools),
        reports,
        admin_snapshot=load_snapshot(args.tools),
        store_snapshot=load_snapshot(args.store_tools),
    )
    print(markdown)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        # GitHub caps a step summary at 1 MiB and drops the whole thing when it
        # is exceeded — silently, with the step still reporting success. The
        # failure detail here scales with the number of failures (prompt plus
        # discovery trail each), so a bad run produces the largest summary and
        # is exactly when it would vanish.
        #
        # Truncating keeps the tables, which are the part worth reading; the
        # byte count goes to stdout either way so "no summary" can be told apart
        # from "summary too big" without another run to find out.
        encoded = (markdown + "\n").encode()
        print(f"::notice::job summary is {len(encoded):,} bytes (GitHub drops anything over {GITHUB_SUMMARY_LIMIT:,})")
        if len(encoded) > GITHUB_SUMMARY_LIMIT:
            note = (
                "\n\n---\n\n**Truncated** — the full summary exceeded GitHub's 1 MiB limit. "
                "See the step log or the uploaded artifact.\n"
            )
            encoded = encoded[: GITHUB_SUMMARY_LIMIT - len(note.encode())] + note.encode()
            print(f"::warning::job summary truncated to {len(encoded):,} bytes")
        with open(summary_path, "ab") as handle:
            handle.write(encoded)
    return 0


def render_skipped(reports: list[dict]) -> str:
    """Fixtures that were not graded, grouped by reason.

    Rendered even when empty is pointless, but rendered *only* when non-empty is
    the point: a suite that stops testing something should have to say so in the
    same place it reports its rate. Otherwise the rate improves because the hard
    cases left, and the summary reads like progress.
    """
    by_reason: dict[str, list[str]] = collections.defaultdict(list)
    for report in reports:
        for skip in report.get("skipped_fixtures") or []:
            by_reason[skip.get("reason", "no reason recorded")].append(skip.get("expected_tool") or skip["id"])
    if not by_reason:
        return ""

    total = sum(len(items) for items in by_reason.values())
    lines = [f"### Not graded ({total} fixtures)", ""]
    for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        tools = ", ".join(f"`{t}`" for t in sorted(set(items)))
        lines.append(f"- **{len(items)}** — {reason}<br><sub>{tools}</sub>")
    lines.append("")
    return "\n".join(lines)


def render_prompt_delta(reports: list[dict]) -> str:
    """What the context prompt is worth, and what each suite actually got.

    Two questions, one table. The first is the A/B: the same model on the same
    fixtures with the server's context prompts on and off, which is the only way
    to say whether ~20k characters of tool guide earn their place.

    The second matters more and is easy to miss — the suites do not get the same
    prompt. The admin endpoint serves four prompts; the store endpoint serves
    none. Their pass rates were being read side by side as though they were the
    same measurement, and nothing in any report said otherwise.
    """
    seen: dict[str, dict] = {}
    for report in reports:
        inventory = report.get("context_prompt") or {}
        if not inventory:
            continue
        rate = _discovery_rate(report)
        # `disabled`, not "has no names": a store run took everything the server
        # offered and the server offered nothing, which is `all` — the endpoint's
        # problem, not the arm's. Deriving from emptiness labelled it `none` and
        # then excluded it from the very note that exists to report it.
        prompt_set = inventory.get("set") or ("none" if inventory.get("disabled") else "all")
        key = f"{report.get('server', '?')}|{report.get('model', '?')}|{prompt_set}"
        seen[key] = {
            "server": report.get("server", "?"),
            "model": report.get("model", "?"),
            "enabled": bool(report.get("system_prompt")),
            "chars": inventory.get("total_chars", 0),
            "names": inventory.get("names", []),
            "set": prompt_set,
            "rate": rate,
            "by_tier": report.get("by_tier") or {},
        }
    if not seen:
        return ""

    lines = ["### Context prompt", "", "| Model | Set | Chars | Prompts | Pass rate |", "|---|---|---:|---|---:|"]
    for entry in sorted(seen.values(), key=lambda e: -e["chars"]):
        names = ", ".join(f"`{n}`" for n in entry["names"]) or "_none_"
        rate = "—" if entry["rate"] is None else f"{round(100 * entry['rate'])}%"
        lines.append(f"| `{entry['model']}` | `{entry['set']}` | {entry['chars']:,} | {names} | {rate} |")
    lines.append("")
    lines += _contamination_table(list(seen.values()))

    for note in _prompt_notes(list(seen.values())):
        lines += [note, ""]
    return "\n".join(lines)


def _discovery_rate(report: dict) -> float | None:
    mode = (report.get("modes") or {}).get("discovery") or {}
    passed, failed = mode.get("passed"), mode.get("failed")
    if passed is None or failed is None or (passed + failed) == 0:
        return None
    return passed / (passed + failed)


def _prompt_notes(entries: list[dict]) -> list[str]:
    """The two readings worth spelling out rather than leaving to inference."""
    notes = []
    # Same model AND same server. Matching on the model alone paired the store
    # suite's prompt-on run against admin's prompt-off run and reported the gap
    # between two different endpoints as what the prompt was worth — a confident,
    # completely wrong number.
    pairs = [
        (a, b)
        for a in entries
        for b in entries
        if a["model"] == b["model"] and a["server"] == b["server"] and a["enabled"] and not b["enabled"]
    ]
    for on, off in pairs:
        if on["rate"] is None or off["rate"] is None:
            continue
        delta = round(100 * (on["rate"] - off["rate"]))
        verdict = "worth it" if delta > 0 else ("no measurable effect" if delta == 0 else "actively hurting")
        notes.append(
            f"**{on['chars']:,} characters of context prompt moved `{on['model']}` by {delta:+d} points — {verdict}.**"
        )
    # An arm we deliberately ran with `--context-prompts none` is not an endpoint
    # that ships nothing — it is the control. Counting it here claimed the store
    # endpoint's problem existed on admin too.
    served = [e for e in entries if e["set"] != "none"]
    if any(not e["names"] for e in served) and any(e["names"] for e in served):
        notes.append(
            "One endpoint ships **no context prompt at all** while another ships several. Their pass rates are "
            "not comparable: a model working the bare endpoint is being asked to do the same job with a fraction "
            "of the guidance."
        )
    return notes


def _contamination_table(entries: list[dict]) -> list[str]:
    """Each area's rate under every prompt set that contained its prompt.

    This is the question the sets exist to answer. A merchant-tools fixture under
    `core+merchant` carries the prompts it needs; under `all` it additionally
    carries 5,615 characters of dev-tools instructions naming tools it must not
    pick. If the rate drops between the two, the extra prompt is not neutral —
    and that failure currently shows up in the scorecard as a description
    problem, attributed to the wrong thing entirely.
    """
    by_set = {e["set"]: e for e in entries if e["set"] != "none"}
    if len(by_set) < 2:
        return []

    areas = sorted({area for e in by_set.values() for area in e["by_tier"]})
    if not areas:
        return []

    sets = sorted(by_set)
    lines = [
        "<details><summary>Per-area rate by prompt set</summary>",
        "",
        "| Area | " + " | ".join(f"`{s}`" for s in sets) + " |",
        "|---" * (len(sets) + 1) + "|",
    ]
    for area in areas:
        cells = []
        for name in sets:
            tier = by_set[name]["by_tier"].get(area)
            cells.append(f"{round(100 * tier['rate'])}%" if tier and tier.get("total") else "—")
        lines.append(f"| {area} | " + " | ".join(cells) + " |")
    lines += ["", "</details>", ""]
    return lines


if __name__ == "__main__":
    sys.exit(main())
