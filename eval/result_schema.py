"""The per-fixture result record — the contract five readers share.

`eval/summary.py`, `eval/scoring.py`, `eval/compare_runs.py`, `eval/report.py`
and `eval/tool_scorecard.py` all index these keys by string. Until now that
contract was implicit: a renamed key surfaced as a `KeyError` mid-report in CI
rather than at author time. `FixtureResult` makes it explicit so a type checker
(basedpyright) flags a typo on the producer side, and `SCHEMA_VERSION` gives the
back-compat branches something to key off instead of sniffing for the presence
of a field.

`total=False` because there are three producers with different subsets:

  * `run_fixture_discovery` — the full record (a graded fixture);
  * `skipped_result` — a fixture we declined to run, with `skip_reason`;
  * `error_result` — a fixture that raised, with `error`.

Bump `SCHEMA_VERSION` when a key's meaning changes or a consumed key is removed,
not for a purely additive field a reader already tolerates via `.get`.
"""

from typing import Required, TypedDict, cast

# 1 is the first version to carry the field at all; rows written before it have
# no `schema_version` key, so a reader treats its absence as version 0.
SCHEMA_VERSION = 1

# An arbitrary decoded-JSON object: a tool's arguments, an MCP result body, a
# fixture's `expect_result`. `object` rather than `Any` on purpose — `Any`
# silences the checker at every use downstream, which is the opposite of the
# point, while `object` forces the narrowing that says what the code actually
# assumes. Use a TypedDict below wherever the shape IS known; this is for the
# genuinely open ones.
type JsonObject = dict[str, object]


class FixtureResult(TypedDict, total=False):
    """One fixture's outcome. See the module docstring for which producer emits
    which subset; every key here is read by at least one of the five consumers."""

    # Required on all three producers, so a reader may bracket-index these
    # without `.get`. `total=False` makes every OTHER key NotRequired; these
    # eight opt back in with Required[...], which also makes pyright reject a
    # producer that forgets one.
    schema_version: Required[int]
    id: Required[str]
    category: Required[str]
    mode: Required[str]
    prompt: Required[str]
    expected_tool: Required[str | None]
    selected_tool: Required[str | None]
    passed: Required[bool]

    # Present only on graded rows (skipped_result omits it).
    expected_toolset: str | None

    # The verdict.
    selected_input: "JsonObject"
    fail_reason: str | None

    # First-answer vs recovery: the scorecard reads first_tool_correct for
    # precision, because `passed` now allows a later attempt to have got there.
    first_tool_correct: bool | None
    first_try: bool
    recovered: bool
    attempted_tools: list["AttemptRecord"]
    wrong_calls: int
    steps_to_correct: int | None

    # Execution facts.
    execution: str | None
    dry_run_forced: bool
    steps: int

    # Discovery navigation.
    meta_calls: list["MetaCall"]
    discovery_path: str
    search_hit: bool | None
    search_rank: int | None
    search_score: float | None
    search_candidates: int | None
    enabled_toolsets: list[str]
    enabled_correct_toolset: bool | None

    # Cost and size.
    latency_s: float
    tokens: "TokenCounts"
    payload_bytes: int
    surface_tokens: int
    surface_tokens_peak: int

    notes: str

    # Attached by the discovery worker after the fixture runs, not by the
    # producers above: `attempts` is the retry count, `_line` the pre-rendered
    # progress line printed as each fixture lands.
    attempts: int
    _line: str

    # Only on the non-graded producers.
    skipped: bool
    skip_reason: str
    error: str


# ---------------------------------------------------------------------------
# The rest of the JSON this repo passes between processes.
#
# Same argument as FixtureResult, applied to the shapes that were still `dict`:
# every one of these crosses a process boundary as a file, so a renamed key is
# invisible until a reader indexes it. Typing them is also what collapses the
# Unknown* diagnostics — a `dict` in a signature makes every member access
# downstream unknown, which is how ownership.py got 29 diagnostics from four
# annotations.
#
# `total=False` throughout, with Required[...] on the keys every producer emits,
# so a reader may bracket-index those and must use `.get` for the rest.
# ---------------------------------------------------------------------------


class TokenCounts(TypedDict, total=False):
    """Both providers report cached tokens, in opposite directions; the adapters
    normalise at capture (see eval/cost.py)."""

    input: Required[int]
    output: Required[int]
    cached_input: int


class AttemptRecord(TypedDict, total=False):
    """One answering call the model made — `attempted_tools` in FixtureResult.

    Recovery is measured over these, so the whole list matters and not just the
    last: `tool` is what was named, `ok` whether the call satisfied the fixture,
    and `error` what the server said (transport error OR the in-band
    `success: false` message, which is the only one merchant tools send).
    """

    tool: Required[str]
    correct: Required[bool]
    step: Required[int]
    executed: bool
    ok: bool
    reason: str | None
    error: str


class MetaCall(TypedDict, total=False):
    """A discovery meta-tool call — `meta_calls`. Executed and fed back, never
    graded; this is what the report renders as the discovery trail."""

    tool: Required[str]
    input: Required[JsonObject]
    result_preview: str


class TierBucket(TypedDict, total=False):
    """One owning repository's slice of a run — `by_tier`.

    `ids` is the full graded set and `failed_ids` the failures, both needed
    because the summary counts a fixture once across suites rather than once per
    fixture-run (see ownership.breakdown).
    """

    passed: Required[int]
    total: Required[int]
    rate: Required[float]
    ids: list[str]
    failed_ids: list[str]


class CostBlock(TypedDict, total=False):
    """What a run cost, priced by pricing.yaml.

    `priced` false means the model is absent from pricing.yaml, which renders as
    "unpriced" rather than $0.00 — free and unknown are different claims.
    """

    model: Required[str]
    priced: Required[bool]
    tokens: Required[TokenCounts]
    total_usd: float | None
    unverified: bool
    verified: str
    graded: int
    passed: int
    # None, not 0.0, when there is nothing to divide by or take a percentile of.
    # An empty graded set costing $0.00 per fixture would read as good news.
    usd_per_fixture: float | None
    usd_per_passing_fixture: float | None
    latency_p50: float | None
    latency_p95: float | None
    payload_bytes_p50: float | None
    payload_bytes_p95: float | None
    surface_tokens_p50: float | None
    surface_tokens_peak: float | None


class SummaryRow(TypedDict, total=False):
    """One eval run's verdict — `results/rows/*.json`, written by
    --summary-row and read by eval/summary.py.

    Separate from the full report because four runs in one job are four
    processes: markdown appended from separate processes cannot form one table,
    so each emits a row and the report job renders them together.
    """

    suite: Required[str]
    provider: Required[str]
    model: Required[str]
    rate: Required[float]
    graded: Required[int]
    gate: Required[str]
    errored: int
    throttled: int
    advisory: bool
    by_tier: dict[str, TierBucket]
    cost: CostBlock


class SkippedFixture(TypedDict):
    """A fixture that was not graded, and why. A pass rate over a denominator
    that silently shrank is the failure this exists to make visible."""

    id: str
    expected_tool: str | None
    reason: str


class ModeBlock(TypedDict, total=False):
    """One arm's results — `modes["discovery"]`, and the triage arms alongside it.

    The diagnostic arms live under the same key deliberately, so compare_runs and
    the gate, which both read modes["discovery"] by name, are untouched by their
    presence.
    """

    passed: Required[int]
    failed: Required[int]
    skipped: Required[int]
    results: Required[list[FixtureResult]]


class Report(TypedDict, total=False):
    """A whole run — `results/eval-*.json`.

    Read back by compare_runs, summary and cost_drift, including across runs: the
    cost baseline is a previous nightly's file, so an additive field has to be
    tolerated by `.get` rather than assumed.
    """

    timestamp: Required[str]
    server: Required[str]
    provider: Required[str]
    model: Required[str]
    modes: Required[dict[str, ModeBlock]]
    fixtures: Required[int]
    system_prompt: bool
    context_prompt: JsonObject
    max_steps: int
    discovery_summary: JsonObject
    skipped_fixtures: list[SkippedFixture]
    by_tier: dict[str, TierBucket]
    cost: CostBlock


class ToolDef(TypedDict, total=False):
    """A tool as the server advertises it, and as tool-search embeds it."""

    name: Required[str]
    description: str
    inputSchema: JsonObject  # noqa: N815 — the wire name, not ours to rename


class Toolset(TypedDict, total=False):
    """A discovery group. `enabled` is per-session and only present on a live
    toolsets-list reply, not in a snapshot."""

    name: Required[str]
    tools: Required[list[str]]
    title: str
    description: str
    enabled: bool


class Snapshot(TypedDict, total=False):
    """A catalogue snapshot — `tool-history/*.json`.

    The committed one is the drift baseline: it holds the descriptions the models
    were actually shown, which is why the workflow refreshes it before the eval
    rather than reading the file the repo happens to have.
    """

    tools: Required[list[ToolDef]]
    toolsets: Required[list[Toolset]]
    default_tools: list[str]
    server_instructions: str


class ToolHealth(TypedDict, total=False):
    """The static layer's verdict on one tool — the values of
    `results/tool-health-<endpoint>.json`.

    Only `fail` withholds fixtures. `skipped` means unproven, not broken.
    """

    status: Required[str]
    reason: str


class FunctionalRecord(TypedDict, total=False):
    """One assertion from a functional run — an element of
    `results/functional-<endpoint>-<ts>.json`'s `tools`.

    `tool == "check"` marks a structural check about the server rather than a
    tool, which is why tool_health() filters on it.
    """

    tool: Required[str]
    label: Required[str]
    status: Required[str]
    preview: str
    error: str
    reason: str


# `pass`/`fail`/`skip` are the historical bash runner's key names and `pass` is a
# keyword, so a whole functional run — `results/functional-<endpoint>-<ts>.json` —
# has to be spelled functionally.
FunctionalReport = TypedDict(
    "FunctionalReport",
    {
        "timestamp": str,
        "server": str,
        "pass": int,
        "fail": int,
        "skip": int,
        "total": int,
        "tools": list[FunctionalRecord],
        "health": dict[str, ToolHealth],
    },
)


class Fixture(TypedDict, total=False):
    """One prompt from eval/fixtures.yaml — the input side of the contract.

    Distinct from FixtureResult, which is the output. `expected_tool` is absent on
    a negative fixture (`expect_no_tool`), which is why every reader that keys on
    it has to use `.get`.

    `unresolved_placeholder` is attached by the runner, not the YAML: a prompt
    naming an id the lane could not supply is skipped rather than graded.
    """

    id: Required[str]
    prompt: Required[str]
    category: str
    expected_tool: str
    expected_toolset: str
    acceptable_tools: list[str]
    expect_no_tool: bool
    expect_result: "str | ExpectSpec"
    max_steps: int
    notes: str
    unresolved_placeholder: str


class MinItems(TypedDict, total=False):
    """The `min_items` predicate: the collection at `path` must hold >= `n`."""

    path: str
    n: int


class ExpectSpec(TypedDict, total=False):
    """A fixture's `expect_result`: a tier plus the predicates that tier turns on.

    Lives here rather than in eval/assertions.py so Fixture can name it without
    importing the module that consumes it.
    """

    tier: str
    has_keys: list[str]
    min_items: MinItems
    contains: list[str]


# `pass` is a keyword, so this one needs the functional form.
PassCount = TypedDict("PassCount", {"pass": int, "total": int})


class Score(TypedDict):
    """Per-tool and per-category pass counts (skipped fixtures excluded)."""

    tools: dict[str, PassCount]
    cats: dict[str, PassCount]


class GateVerdict(TypedDict):
    """The three independent axes a run is judged on, deliberately not folded
    together: quality (the overall rate), core (its own denominator), and
    validity (too many fixtures never reached the model, so the run is missing
    data rather than reporting a bad model).
    """

    graded: list[FixtureResult]
    gating: list[FixtureResult]
    errored: int
    error_rate: float
    passed: int
    rate: float
    core_passed: int
    core_total: int
    core_rate: float
    min_core: float
    quality_ok: bool
    core_ok: bool
    run_valid: bool
    ok: bool


class DiscoverySummary(TypedDict, total=False):
    """The aggregate block a report carries under `discovery_summary`.

    Every key NotRequired because recovery_summary's fields are merged in and it
    returns nothing at all when no fixture carries the recovery fields — reports
    written before that existed are still read by compare_runs.
    """

    fixtures: int
    skipped: int
    passed: int
    avg_steps: float
    max_steps_hit: int
    path_distribution: dict[str, int]
    search_used: int
    search_hit_rate: float | None
    search_rank_p50: int | None
    search_rank_worst: int | None
    toolset_enable_graded: int
    toolset_enable_correct: int
    tokens: TokenCounts
    # From recovery_summary.
    first_try_rate: float
    recovery_rate: float | None
    recovered: int
    avg_wrong_calls: float
    avg_steps_to_correct: float | None
    dry_run_forced: int
    unexecuted: int


class ContentBlock(TypedDict, total=False):
    """One block of a tools/call result. `type` defaults to "text" when absent,
    which is why readers compare with a default rather than requiring the key."""

    type: str
    text: str


class McpError(TypedDict, total=False):
    """A protocol-level error. Distinct from a TOOL-level failure, which arrives
    as a normal result with `isError` set — or, for the merchant and UCP tools,
    as `success: false` inside the text block with no error field at all."""

    code: int
    message: str


class McpResult(TypedDict, total=False):
    """The `result` of any MCP reply this repo makes.

    One type for every method rather than one per method: the callers already
    branch on which keys are present, and every key here is NotRequired, so a
    reader that asks for `tools` on a tools/call reply gets None instead of a
    type error about a shape it could not have known statically.
    """

    content: list[ContentBlock]
    isError: bool  # noqa: N815 — the wire name
    _meta: JsonObject
    instructions: str
    tools: list[ToolDef]
    nextCursor: str | None  # noqa: N815 — the wire name
    prompts: list["PromptRef"]
    messages: list["PromptMessage"]


class McpResponse(TypedDict, total=False):
    """One JSON-RPC reply from the MCP endpoint.

    Every key NotRequired because the three shapes share this type: a `result`
    reply, an `error` reply, and the SSE frames a streamable-HTTP server pushes
    when it also has a notification to send. Readers ask which one arrived rather
    than assuming (see mcp_call_error, mcp_result_text).
    """

    jsonrpc: str
    id: int
    result: McpResult
    error: McpError


class McpHeaders(TypedDict, total=False):
    """The auth headers an endpoint sends. Admin uses the integration pair, the
    Store endpoint a sales-channel key — NOT OAuth, in either case."""

    sw_access_key: str
    sw_secret_access_key: str
    sw_sc_access_key: str


class PromptRef(TypedDict, total=False):
    """A context prompt as prompts/list advertises it."""

    name: Required[str]
    description: str


class PromptMessage(TypedDict, total=False):
    """One message of a prompts/get reply. `content` is a block on every server
    seen so far, but the reader tolerates a bare string — the spec allows it and
    guessing wrong costs the whole prompt."""

    role: str
    content: "ContentBlock | str"


class PromptInventory(TypedDict, total=False):
    """What the context prompt was made of, recorded alongside the run.

    The flag alone was not enough: two runs with the same boolean can have been
    given different prompts, and `available` vs `names` is what distinguishes a
    deliberately narrowed run from an endpoint that ships nothing — the store
    endpoint really does ship nothing.
    """

    names: Required[list[str]]
    chars: Required[dict[str, int]]
    instructions_chars: int
    available: list[str]
    excluded: list[str]
    total_chars: int
    sha256: str
    # Which --context-prompts set produced this, and whether the arm withheld the
    # prompt entirely. `disabled` is not "no names": the store endpoint serves
    # nothing, which is a different fact from an arm that turned the prompt off.
    set: str
    disabled: bool


# ---------------------------------------------------------------------------
# Coercion at the boundary.
#
# `JsonObject` is dict[str, object] rather than dict[str, Any] on purpose: Any
# spreads, so one untyped decode used to leave every reader downstream untyped
# too. The cost is that narrowing an `object` with isinstance gives
# dict[Unknown, Unknown], which is no better. These two state the key and value
# types once, at the single point where the shape is actually checked.
# ---------------------------------------------------------------------------


def as_object(value: object) -> JsonObject:
    """A decoded JSON value as a string-keyed map, or {} if it was not one."""
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """A decoded JSON value as a list, or [] if it was not one."""
    return cast(list[object], value) if isinstance(value, list) else []


class ModelPrice(TypedDict, total=False):
    """$/1M tokens for one model, from pricing.yaml.

    `cached_input` is separate because both providers bill it differently and at
    this suite's cache-hit rate it is most of the bill.
    """

    input: float
    output: float
    cached_input: float
    unverified: bool


class Pricing(TypedDict, total=False):
    """pricing.yaml as a whole: per-model rates plus the date they were checked.

    Hand-maintained, so `verified` is the honest part — a rate nobody has
    re-read is still a guess, and the summary marks it with an asterisk.
    """

    verified: str
    models: dict[str, ModelPrice]
    ci_usd_per_minute: float


class CombinedCost(TypedDict, total=False):
    """Several runs' costs rolled into the job headline.

    An unpriced run still contributes tokens — those are always known — but
    leaves the dollar total incomplete, which the caller says out loud rather
    than rounding away.
    """

    tokens: Required[TokenCounts]
    total_usd: Required[float]
    complete: Required[bool]
    unpriced_models: Required[list[str]]
    unverified_models: Required[list[str]]


class ScorecardAccumulator(TypedDict):
    """The counters scorecard() fills while walking the results.

    Separate from ScorecardEntry because they are genuinely different shapes: this
    carries the raw tallies (expected_passed, search_ranks) that the output rows
    turn into rates and drop. Everything Required — bucket() creates them all at
    once — so the accumulation loop may bracket-index without a default per field.
    """

    expected_n: int
    expected_passed: int
    selected_n: int
    selected_correct: int
    confused_with: dict[str, int]
    steals_from: dict[str, int]
    false_positives_on_negatives: int
    search_ranks: list[int]


class ScorecardEntry(TypedDict):
    """One rendered row: recall, precision and who this tool traded fixtures with.

    Recall is what the pass rate already reports. Precision is the half that
    catches an over-broad description — a tool winning its own fixtures AND its
    siblings' scores 100% recall while making the catalogue worse.

    `confused_with` is who took this tool's fixtures; `steals_from` is who it took
    from. Both directions, because they are different findings.
    """

    expected_n: int
    recall: float | None
    selected_n: int
    precision: float | None
    f1: float | None
    confused_with: dict[str, int]
    steals_from: dict[str, int]
    false_positives_on_negatives: int
    search_rank_p50: float | None
    description_chars: int
    flags: list[str]


class Collision(TypedDict):
    """A confusion pair, reported once regardless of direction. `mutual` marks the
    strongest signal: two descriptions that each attract the other's prompts."""

    pair: tuple[str, str]
    total: int
    directions: dict[str, int]
    mutual: bool


class RunSide(TypedDict, total=False):
    """One model's half of the cross-model comparison."""

    model: str
    passed: int
    total: int
    rate: float
    errored: int


class Comparison(TypedDict, total=False):
    """The cross-model split — `results/eval-comparison.json`.

    `both_fail` is the only actionable set: a fixture both models miss points at
    the tool description, one only the weaker misses is its capability gap, and
    one only the stronger misses is usually flaky discovery.
    """

    primary: RunSide
    second: RunSide
    shared: int
    both_pass: list[str]
    only_primary: list[str]
    only_second: list[str]
    both_fail: list[str]
    both_fail_by_tool: dict[str, list[str]]
    both_fail_detail: list["BothFailDetail"]
    unmatched: list[str]
    gate: JsonObject


class PromptArm(TypedDict, total=False):
    """One (server, model, prompt-set) arm, for the context-prompt table.

    The suites do not receive the same prompt — admin serves four, the store
    endpoint serves none — and their pass rates were being read side by side as
    though they were the same measurement.
    """

    server: str
    model: str
    enabled: bool
    chars: int
    names: list[str]
    set: str
    rate: float | None
    by_tier: dict[str, TierBucket]


class BothFailDetail(TypedDict, total=False):
    """One fixture both models missed, with everything needed to act on it.

    The id lists alone required cross-referencing two reports by hand, which is
    the step that stopped anyone acting on these rows. `descriptions` is keyed by
    tool name rather than by role because the two models often pick the same
    wrong tool, and repeating its description would double the block for nothing.
    """

    id: Required[str]
    expected_tool: str
    primary_selected: str | None
    second_selected: str | None
    primary_reason: str | None
    second_reason: str | None
    prompt: str
    category: str
    notes: str
    expected_toolset: str | None
    descriptions: dict[str, str]
    primary_trail: str
    second_trail: str


class CatalogueFacts(TypedDict):
    """The uniform properties of a catalogue, counted once.

    Reported at this level rather than per tool because they ARE uniform: the
    undocumented-parameter check fired on 102 of 102 parameters, and thirty
    identical findings is noise where one fact is a finding.
    """

    tools: int
    params: int
    params_undocumented: int
    string_params: int
    string_params_unconstrained: int
    description_tokens: int
    schema_tokens: int


class ToolLintEntry(TypedDict):
    """One tool's static description findings."""

    findings: list[str]
    description_chars: int
    schema_tokens: int


class SimilarPair(TypedDict):
    """Two descriptions and how alike they read.

    Advisory only, and a weak signal: measured against confirmed confusions it
    ranks most of them in the top 15% of all pairs, but the top of the list is
    dominated by pairs that have never actually been confused.
    """

    pair: tuple[str, str]
    similarity: float


class LintReport(TypedDict):
    """What toollint produces for a whole snapshot."""

    facts: CatalogueFacts
    tools: dict[str, ToolLintEntry]
    similar_pairs: list[SimilarPair]


class ToolsetChanges(TypedDict):
    """Toolset membership movement between two snapshots."""

    added: list[str]
    removed: list[str]
    membership: list[str]


class DriftSummary(TypedDict):
    """Structured diff of two catalogue snapshots.

    `default_surface` is called out separately because a change there is
    categorically worse than a description edit: it moves what every fixture has
    to discover before it can call anything.
    """

    added: list[str]
    removed: list[str]
    described: list[str]
    schema: list[str]
    default_surface: bool
    toolsets: ToolsetChanges
    instructions: bool


class CostFinding(TypedDict):
    """One metric that moved more than the threshold, in either direction.

    Improvements are reported too: a sudden halving is as much a signal as a
    doubling, and is the shape a silently-broken run takes — fewer steps because
    discovery stopped happening at all.
    """

    metric: str
    label: str
    meaning: str
    previous: float
    current: float
    change: float
