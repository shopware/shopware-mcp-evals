"""Structural checks on the eval fixture files.

These run without a server: they validate the fixtures against each other and
against the committed tool snapshot. The coverage check is the important one —
it fails when the server grows a tool that nobody wrote prompts for, which is
the drift a purely manual review keeps missing.
"""

import collections
import json
import re
from pathlib import Path
from typing import cast

import pytest
import yaml

from eval.result_schema import Fixture, Snapshot, as_list, as_object

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tool-history" / "latest.json"

CATEGORIES = {"unambiguous", "disambiguation", "chain", "meta", "discovery", "negative"}

# Fixtures where the right answer is that no tool applies. They carry no
# `expected_tool`, so every check keyed on one has to skip them.
NEGATIVE = "negative"

# Every tool needs at least this many prompts. Three is the point where a pass
# stops being attributable to one lucky phrasing.
MIN_PROMPTS_PER_TOOL = 3


def _load(name: str) -> list[Fixture]:
    loaded = as_object(cast(object, yaml.safe_load((ROOT / "eval" / name).read_text())))
    return [cast(Fixture, cast(object, as_object(f))) for f in as_list(loaded.get("fixtures"))]


def _read_snapshot(path: Path) -> Snapshot:
    return cast(Snapshot, cast(object, as_object(cast(object, json.loads(path.read_text())))))


ADMIN = _load("fixtures.yaml")
STORE = _load("fixtures_store.yaml")
ALL_FILES = [("admin", ADMIN), ("store", STORE)]

_snapshot = _read_snapshot(SNAPSHOT)
SNAPSHOT_TOOLS = {t["name"] for t in _snapshot["tools"]}
DEFAULT_TOOLS = set(_snapshot.get("default_tools", []))
TOOLSET_OF = {tool: ts["name"] for ts in _snapshot["toolsets"] for tool in ts["tools"]}


def _positive(fixtures: list[Fixture]) -> list[Fixture]:
    return [f for f in fixtures if f.get("category") != NEGATIVE]


def _expected(fixture: Fixture) -> str:
    """The tool a positive fixture names.

    A negative fixture names none, which is the whole point of it — so this is
    only ever called on the output of `_positive`, and says so rather than
    letting a `str | None` leak into every set comparison below.
    """
    tool = fixture.get("expected_tool")
    assert tool, f"{fixture['id']} is not a negative fixture but names no tool"
    return tool


def _counts(fixtures: list[Fixture]) -> collections.Counter[str]:
    return collections.Counter(_expected(f) for f in _positive(fixtures))


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_required_fields_are_present(label: str, fixtures: list[Fixture]) -> None:
    for fixture in fixtures:
        fields = (
            ("id", "category", "prompt")
            if fixture.get("category") == NEGATIVE
            else ("id", "category", "prompt", "expected_tool")
        )
        for field in fields:
            assert fixture.get(field), f"{label}: fixture {fixture.get('id')!r} is missing {field}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_negative_fixtures_declare_themselves_and_name_no_tool(label: str, fixtures: list[Fixture]) -> None:
    """`expect_no_tool` is what the runner and scorer key on, so the category
    label alone is not enough — and a negative carrying an `expected_tool` would
    be scored against a tool it is meant to prove nothing should reach."""
    for fixture in fixtures:
        if fixture.get("category") != NEGATIVE:
            continue
        assert fixture.get("expect_no_tool") is True, f"{label}: {fixture['id']} must set expect_no_tool: true"
        assert not fixture.get("expected_tool"), f"{label}: {fixture['id']} is negative but names an expected_tool"
        assert not fixture.get("expected_toolset"), f"{label}: {fixture['id']} is negative but names a toolset"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_only_negative_fixtures_set_expect_no_tool(label: str, fixtures: list[Fixture]) -> None:
    stray = [f["id"] for f in fixtures if f.get("expect_no_tool") and f.get("category") != NEGATIVE]
    assert not stray, f"{label}: expect_no_tool outside the negative category: {stray}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_negative_fixtures_explain_what_would_answer_them(label: str, fixtures: list[Fixture]) -> None:
    """A negative expires the moment the server grows the capability it probes.

    The note is what a reviewer reads on the drift PR to decide whether a newly
    added tool has just turned this fixture into a false accusation.
    """
    thin = [f["id"] for f in fixtures if f.get("category") == NEGATIVE and len((f.get("notes") or "").strip()) < 40]
    assert not thin, f"{label}: negative fixtures need notes saying what capability would answer them: {thin}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_categories_are_known(label: str, fixtures: list[Fixture]) -> None:
    unknown = {f["id"]: f.get("category") for f in fixtures if f.get("category") not in CATEGORIES}
    assert not unknown, f"{label}: unknown categories {unknown}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_ids_are_unique(label: str, fixtures: list[Fixture]) -> None:
    dupes = [i for i, n in collections.Counter(f["id"] for f in fixtures).items() if n > 1]
    assert not dupes, f"{label}: duplicate fixture ids {dupes} — results are keyed by id"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_prompts_are_unique(label: str, fixtures: list[Fixture]) -> None:
    """A copy-pasted prompt inflates the per-tool count without adding coverage."""
    seen: dict[str, list[str]] = collections.defaultdict(list)
    for fixture in fixtures:
        seen[fixture["prompt"].strip().lower()].append(fixture["id"])
    dupes = {p: ids for p, ids in seen.items() if len(ids) > 1}
    assert not dupes, f"{label}: duplicate prompts {dupes}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_toolset_is_declared_for_non_meta_fixtures(label: str, fixtures: list[Fixture]) -> None:
    """Discovery mode grades toolset-enable, so every non-meta fixture needs a target."""
    missing = [f["id"] for f in _positive(fixtures) if f.get("category") != "meta" and not f.get("expected_toolset")]
    assert not missing, f"{label}: non-meta fixtures without expected_toolset: {missing}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_meta_fixtures_declare_no_toolset(label: str, fixtures: list[Fixture]) -> None:
    """Meta-tools are on the default surface — enabling a toolset to reach them is wrong."""
    stray = [f["id"] for f in fixtures if f.get("category") == "meta" and f.get("expected_toolset")]
    assert not stray, f"{label}: meta fixtures must not set expected_toolset: {stray}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_every_tool_has_enough_prompts(label: str, fixtures: list[Fixture]) -> None:
    thin = {t: n for t, n in _counts(fixtures).items() if n < MIN_PROMPTS_PER_TOOL}
    assert not thin, f"{label}: tools below {MIN_PROMPTS_PER_TOOL} prompts: {thin}"


# --- Admin-only: cross-checked against the committed tool snapshot ----------
# The store endpoint has no committed snapshot, so its fixtures get the
# structural checks above only.


def test_admin_fixtures_reference_known_tools() -> None:
    """Catches a typo or a tool renamed server-side."""
    named = {_expected(f) for f in _positive(ADMIN)}
    named |= {t for f in ADMIN for t in f.get("acceptable_tools", [])}
    unknown = sorted(named - SNAPSHOT_TOOLS)
    assert not unknown, f"tools not in {SNAPSHOT.name}: {unknown}"


def test_admin_fixtures_declare_the_right_toolset() -> None:
    """A fixture pointing at the wrong group would grade toolset-enable incorrectly."""
    wrong = {
        f["id"]: (f.get("expected_toolset"), TOOLSET_OF.get(_expected(f)))
        for f in _positive(ADMIN)
        if f.get("expected_toolset") and TOOLSET_OF.get(_expected(f)) != f.get("expected_toolset")
    }
    assert not wrong, f"expected_toolset disagrees with the snapshot (declared, actual): {wrong}"


def test_admin_meta_fixtures_target_default_surface_tools() -> None:
    off_surface = [
        f["id"] for f in ADMIN if f.get("category") == "meta" and f.get("expected_tool") not in DEFAULT_TOOLS
    ]
    assert not off_surface, f"meta fixtures targeting deferred tools: {off_surface}"


def test_every_advertised_admin_tool_is_covered() -> None:
    """The drift guard: a new server-side tool must arrive with prompts."""
    counts = _counts(ADMIN)
    uncovered = {t: counts.get(t, 0) for t in sorted(SNAPSHOT_TOOLS) if counts.get(t, 0) < MIN_PROMPTS_PER_TOOL}
    assert not uncovered, f"tools in {SNAPSHOT.name} with fewer than {MIN_PROMPTS_PER_TOOL} prompts: {uncovered}"


# ---------------------------------------------------------------------------
# Prompt hygiene — the fixtures must not hand the model the answer
# ---------------------------------------------------------------------------
# Only `prompt` reaches the model: eval/runner.py builds the user message from it
# and nothing else, and `notes` goes into the result record for reporting. So the
# YAML comments and notes cannot influence a run — but a prompt naming its own
# tool would, and it would turn a description test into a string match.
@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_no_prompt_names_the_tool_it_expects(label: str, fixtures: list[Fixture]) -> None:
    """A prompt containing a tool name tests string matching, not description
    quality. The whole design is user vocabulary that avoids the tool's own
    words — see the coverage rule at the top of fixtures.yaml."""
    offenders = [(f["id"], name) for f in fixtures for name in SNAPSHOT_TOOLS if name.lower() in f["prompt"].lower()]

    assert not offenders, f"{label}: prompts naming a tool outright: {offenders}"


# Toolset names are ordinary domain words — `order`, `media`, `entity`, `theme` —
# so a prompt "matching" one is usually just English ("Mark order 10000 as
# shipped"). Only the meta fixtures may name a group on purpose, because
# resolving a named group IS what shopware-toolset-enable does.
ALLOWED_TOOLSET_MENTIONS = {"meta_enable_dev_logs", "meta_enable_media_group"}


SNAPSHOT_TOOLSETS = {ts["name"] for ts in _snapshot["toolsets"]}


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_only_meta_fixtures_name_a_toolset_as_a_toolset(label: str, fixtures: list[Fixture]) -> None:
    """Guards the narrower thing that would be cheating: handing over a group's
    key next to the word "toolset"/"group", which routes discovery for free."""
    offenders: list[tuple[str, str]] = []
    for f in fixtures:
        prompt = f["prompt"].lower()
        for ts in SNAPSHOT_TOOLSETS:
            # The key next to a grouping word, e.g. "the dev-logs toolset".
            if re.search(rf"\b{re.escape(ts.lower())}\b\s*(toolset|tool group|group)", prompt):
                if f["id"] not in ALLOWED_TOOLSET_MENTIONS:
                    offenders.append((f["id"], ts))

    assert not offenders, (
        f"{label}: "
        f"prompts routing discovery by naming a toolset: {offenders}. "
        f"Add to ALLOWED_TOOLSET_MENTIONS only if that is the point of the fixture."
    )


# ---------------------------------------------------------------------------
# Store fixtures, once the Store catalogue has been snapshotted
# ---------------------------------------------------------------------------
# These are inert until tool-history/store.json exists, and that is the point.
# The Store endpoint has never had a snapshot, so nothing could check its
# fixtures against reality — which is how 39 of them came to declare a toolset
# named `shopware`, and 3 `store-api`, when the real ones are shopware-ucp-cart,
# -catalog and -checkout. The isolated triage arm enabled `shopware`, got zero
# tools, and every Store failure was then diagnosed as a description problem.
#
# The moment the snapshot lands these turn on and fail until the fixtures are
# corrected against it.
STORE_SNAPSHOT = ROOT / "tool-history" / "store.json"
store_snapshot_required = pytest.mark.skipif(
    not STORE_SNAPSHOT.exists(),
    reason="tool-history/store.json not committed yet — the nightly reconciliation PR adds it",
)


def _store_snapshot() -> Snapshot:
    return _read_snapshot(STORE_SNAPSHOT)


@store_snapshot_required
def test_store_fixtures_reference_known_tools() -> None:
    tools = {t["name"] for t in _store_snapshot()["tools"]}
    named = {_expected(f) for f in _positive(STORE)}
    named |= {t for f in STORE for t in f.get("acceptable_tools", [])}

    assert not sorted(named - tools), f"tools not in {STORE_SNAPSHOT.name}: {sorted(named - tools)}"


@store_snapshot_required
def test_store_fixtures_declare_a_toolset_that_exists() -> None:
    """The check that would have caught `expected_toolset: shopware` on day one."""
    known = {ts["name"] for ts in _store_snapshot()["toolsets"]}
    wrong = {f["id"]: f.get("expected_toolset") for f in _positive(STORE) if f.get("expected_toolset") not in known}

    assert not wrong, f"toolsets that do not exist on the Store endpoint: {wrong}"


@store_snapshot_required
def test_store_fixtures_declare_the_toolset_their_tool_is_actually_in() -> None:
    toolset_of = {tool: ts["name"] for ts in _store_snapshot()["toolsets"] for tool in ts["tools"]}
    wrong = {
        f["id"]: (f.get("expected_toolset"), toolset_of.get(_expected(f)))
        for f in _positive(STORE)
        if f.get("expected_toolset") and toolset_of.get(_expected(f)) != f.get("expected_toolset")
    }

    assert not wrong, f"expected_toolset disagrees with the snapshot (declared, actual): {wrong}"


# ---------------------------------------------------------------------------
# The shape of a hand-written id.
#
# Grading executes the call, so a value in a prompt reaches the server. Shopware
# generates TWO shapes and a phantom has to match the right one — one hex literal
# used to serve as a UCP cart id (right kind) and an admin cart token (wrong
# kind) in the same breath, and it was not a valid UUID either.
#
# Verified against trunk rather than guessed:
#   src/Core/Framework/Uuid/Uuid.php            randomHex() = bin2hex(UnixTimeGenerator)
#                                               -> UUIDv7; VALID_PATTERN ^[0-9a-f]{32}$
#   src/Core/Framework/Util/Random.php          getAlphanumericString(): a-zA-Z0-9
#   .../SalesChannelContextService.php          cart token = Random::getAlphanumericString(32)
#   SwagAgenticCommerce ContextTokenGenerator   UCP cart/checkout id = Uuid::randomHex()
#
# Version nibble `4` OR `7` is accepted: trunk writes v7, and demodata rows
# already in a shop are v4, so both read as real to a model.
UUID_HEX = re.compile(r"^[0-9a-f]{12}[47][0-9a-f]{3}[89ab][0-9a-f]{15}$")
ALNUM_TOKEN = re.compile(r"^(?=.*[A-Z])(?=.*[0-9])[0-9A-Za-z]{32}$")

# Fixtures whose prompt carries an ADMIN cart token — the one non-hex shape.
# merchant-cart-manage is toolclass.UNSAFE so it is never executed, which is why
# these keep a literal instead of taking `{cart_token}`.
ADMIN_CART_TOKEN_FIXTURES = {"cart_manage_add", "cart_manage_remove", "disambig_cart_manage_quantity"}


def _literals(prompt: str) -> list[str]:
    """Every 32-character id-ish run in a prompt, placeholders excluded."""
    return re.findall(r"\b[0-9A-Za-z]{32}\b", prompt)


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_hand_written_ids_have_a_shape_shopware_actually_generates(label: str, fixtures: list[Fixture]) -> None:
    """A phantom id has to be indistinguishable from a real one.

    Not cosmetic: `Uuid::VALID_PATTERN` accepts any 32 hex, so a bad version
    nibble will not fail a call — it just feeds the model an id no shop would
    ever produce, in a suite whose entire subject is how the model behaves on
    realistic input. The kind being wrong (hex where a cart token belongs) is
    the harder failure: the tool rejects it by shape, which the `accepted` tier
    does not forgive.
    """
    offenders = [
        (f["id"], lit)
        for f in fixtures
        for lit in _literals(f["prompt"])
        if not (UUID_HEX.match(lit) or ALNUM_TOKEN.match(lit))
    ]

    assert not offenders, f"{label}: ids Shopware would never generate: {offenders}"


def test_the_admin_cart_fixtures_carry_a_cart_token_and_not_a_uuid() -> None:
    """The kind being wrong is the harder failure, and the one that actually
    happened: a hex literal stood in for an admin cart token, which
    `Random::getAlphanumericString` never produces. These prompts hold both
    shapes at once — a cart token and a product or line-item id — so the rule is
    that exactly one of them is the non-hex one.
    """
    wrong = {}
    for f in ADMIN:
        if f["id"] not in ADMIN_CART_TOKEN_FIXTURES:
            continue
        lits = _literals(f["prompt"])
        tokens = [lit for lit in lits if ALNUM_TOKEN.match(lit) and not UUID_HEX.match(lit)]
        if len(tokens) != 1:
            wrong[f["id"]] = lits

    assert not wrong, f"expected exactly one alphanumeric cart token per fixture, got: {wrong}"


def test_the_admin_cart_token_fixtures_still_exist() -> None:
    """ADMIN_CART_TOKEN_FIXTURES above is the only place that knows a prompt
    carries the non-hex shape. A renamed fixture would silently drop back to
    being checked as a UUID and pass for the wrong reason."""
    ids = {f["id"] for f in ADMIN}

    assert ADMIN_CART_TOKEN_FIXTURES <= ids, f"stale ids: {sorted(ADMIN_CART_TOKEN_FIXTURES - ids)}"
