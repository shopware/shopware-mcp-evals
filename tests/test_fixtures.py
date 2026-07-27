"""Structural checks on the eval fixture files.

These run without a server: they validate the fixtures against each other and
against the committed tool snapshot. The coverage check is the important one —
it fails when the server grows a tool that nobody wrote prompts for, which is
the drift a purely manual review keeps missing.
"""

import collections
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tool-history" / "latest.json"

CATEGORIES = {"unambiguous", "disambiguation", "chain", "meta", "discovery"}

# Every tool needs at least this many prompts. Three is the point where a pass
# stops being attributable to one lucky phrasing.
MIN_PROMPTS_PER_TOOL = 3


def _load(name):
    return yaml.safe_load((ROOT / "eval" / name).read_text())["fixtures"]


ADMIN = _load("fixtures.yaml")
STORE = _load("fixtures_store.yaml")
ALL_FILES = [("admin", ADMIN), ("store", STORE)]

_snapshot = json.loads(SNAPSHOT.read_text())
SNAPSHOT_TOOLS = {t["name"] for t in _snapshot["tools"]}
DEFAULT_TOOLS = set(_snapshot["default_tools"])
TOOLSET_OF = {tool: ts["name"] for ts in _snapshot["toolsets"] for tool in ts["tools"]}


def _counts(fixtures):
    return collections.Counter(f["expected_tool"] for f in fixtures)


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_required_fields_are_present(label, fixtures):
    for fixture in fixtures:
        for field in ("id", "category", "prompt", "expected_tool"):
            assert fixture.get(field), f"{label}: fixture {fixture.get('id')!r} is missing {field}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_categories_are_known(label, fixtures):
    unknown = {f["id"]: f["category"] for f in fixtures if f["category"] not in CATEGORIES}
    assert not unknown, f"{label}: unknown categories {unknown}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_ids_are_unique(label, fixtures):
    dupes = [i for i, n in collections.Counter(f["id"] for f in fixtures).items() if n > 1]
    assert not dupes, f"{label}: duplicate fixture ids {dupes} — results are keyed by id"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_prompts_are_unique(label, fixtures):
    """A copy-pasted prompt inflates the per-tool count without adding coverage."""
    seen = collections.defaultdict(list)
    for fixture in fixtures:
        seen[fixture["prompt"].strip().lower()].append(fixture["id"])
    dupes = {p: ids for p, ids in seen.items() if len(ids) > 1}
    assert not dupes, f"{label}: duplicate prompts {dupes}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_toolset_is_declared_for_non_meta_fixtures(label, fixtures):
    """Discovery mode grades toolset-enable, so every non-meta fixture needs a target."""
    missing = [f["id"] for f in fixtures if f["category"] != "meta" and not f.get("expected_toolset")]
    assert not missing, f"{label}: non-meta fixtures without expected_toolset: {missing}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_meta_fixtures_declare_no_toolset(label, fixtures):
    """Meta-tools are on the default surface — enabling a toolset to reach them is wrong."""
    stray = [f["id"] for f in fixtures if f["category"] == "meta" and f.get("expected_toolset")]
    assert not stray, f"{label}: meta fixtures must not set expected_toolset: {stray}"


@pytest.mark.parametrize("label,fixtures", ALL_FILES)
def test_every_tool_has_enough_prompts(label, fixtures):
    thin = {t: n for t, n in _counts(fixtures).items() if n < MIN_PROMPTS_PER_TOOL}
    assert not thin, f"{label}: tools below {MIN_PROMPTS_PER_TOOL} prompts: {thin}"


# --- Admin-only: cross-checked against the committed tool snapshot ----------
# The store endpoint has no committed snapshot, so its fixtures get the
# structural checks above only.


def test_admin_fixtures_reference_known_tools():
    """Catches a typo or a tool renamed server-side."""
    named = {f["expected_tool"] for f in ADMIN}
    named |= {t for f in ADMIN for t in f.get("acceptable_tools", [])}
    unknown = sorted(named - SNAPSHOT_TOOLS)
    assert not unknown, f"tools not in {SNAPSHOT.name}: {unknown}"


def test_admin_fixtures_declare_the_right_toolset():
    """A fixture pointing at the wrong group would grade toolset-enable incorrectly."""
    wrong = {
        f["id"]: (f["expected_toolset"], TOOLSET_OF.get(f["expected_tool"]))
        for f in ADMIN
        if f.get("expected_toolset") and TOOLSET_OF.get(f["expected_tool"]) != f["expected_toolset"]
    }
    assert not wrong, f"expected_toolset disagrees with the snapshot (declared, actual): {wrong}"


def test_admin_meta_fixtures_target_default_surface_tools():
    off_surface = [f["id"] for f in ADMIN if f["category"] == "meta" and f["expected_tool"] not in DEFAULT_TOOLS]
    assert not off_surface, f"meta fixtures targeting deferred tools: {off_surface}"


def test_every_advertised_admin_tool_is_covered():
    """The drift guard: a new server-side tool must arrive with prompts."""
    counts = _counts(ADMIN)
    uncovered = {t: counts.get(t, 0) for t in sorted(SNAPSHOT_TOOLS) if counts.get(t, 0) < MIN_PROMPTS_PER_TOOL}
    assert not uncovered, f"tools in {SNAPSHOT.name} with fewer than {MIN_PROMPTS_PER_TOOL} prompts: {uncovered}"
