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

from typing import Any, Required, TypedDict

# 1 is the first version to carry the field at all; rows written before it have
# no `schema_version` key, so a reader treats its absence as version 0.
SCHEMA_VERSION = 1


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
    selected_input: dict[str, Any]
    fail_reason: str | None

    # First-answer vs recovery: the scorecard reads first_tool_correct for
    # precision, because `passed` now allows a later attempt to have got there.
    first_tool_correct: bool | None
    first_try: bool
    recovered: bool
    attempted_tools: list[dict[str, Any]]
    wrong_calls: int
    steps_to_correct: int | None

    # Execution facts.
    execution: str | None
    dry_run_forced: bool
    steps: int

    # Discovery navigation.
    meta_calls: list[dict[str, Any]]
    discovery_path: str
    search_hit: bool | None
    search_rank: int | None
    search_score: float | None
    search_candidates: int | None
    enabled_toolsets: list[str]
    enabled_correct_toolset: bool | None

    # Cost and size.
    latency_s: float
    tokens: dict[str, int]
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
