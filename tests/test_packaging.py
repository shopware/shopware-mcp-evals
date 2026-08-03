"""The package metadata against the files actually in the repo.

`[tool.setuptools] py-modules` is an exhaustive list, not something setuptools
infers. A new top-level module that is not named there is absent from
`pip install -e .` — and locally that stays invisible, because an editable
install made *before* the file existed keeps resolving it while the metadata is
already wrong. CI installs fresh, so CI is where it surfaces.

That is not hypothetical: `lane.py` shipped unlisted and 14 test modules failed
collection with `ModuleNotFoundError: No module named 'lane'` in a run whose
local suite was green.

Reading pyproject.toml rather than the installed distribution on purpose — the
question is whether the declaration matches the source tree, and an answer
derived from an install this same declaration produced could not detect a
mismatch.
"""

import tomllib
from pathlib import Path
from typing import cast

from eval.result_schema import JsonObject, as_list, as_object

ROOT = Path(__file__).resolve().parents[1]

# Root-level modules that are deliberately not importable library code.
NOT_LIBRARY: set[str] = set()


def _pyproject() -> JsonObject:
    return cast(JsonObject, tomllib.loads((ROOT / "pyproject.toml").read_text()))


def _declared(*path: str) -> list[str]:
    """A list of names out of pyproject.toml, by key path."""
    node: object = _pyproject()
    for key in path:
        node = as_object(node).get(key)
    return [str(v) for v in as_list(node)]


def _root_modules():
    return {p.stem for p in ROOT.glob("*.py") if not p.stem.startswith("_")} - NOT_LIBRARY


def test_every_root_module_is_declared_for_the_install() -> None:
    declared = set(_declared("tool", "setuptools", "py-modules"))
    missing = sorted(_root_modules() - declared)

    assert not missing, (
        f"top-level modules absent from [tool.setuptools] py-modules in pyproject.toml: {missing}. "
        "They import fine from the repo root and are missing from a fresh `pip install -e .`, "
        "so this only fails in CI unless it fails here."
    )


def test_no_declared_module_has_been_deleted() -> None:
    """The other direction: a stale name makes the build fail outright rather
    than silently, but the message names setuptools internals rather than the
    file somebody removed."""
    declared = set(_declared("tool", "setuptools", "py-modules"))
    stale = sorted(declared - _root_modules())

    assert not stale, f"py-modules names modules that no longer exist: {stale}"


def test_the_coverage_source_list_covers_the_root_modules() -> None:
    """A module missing here reports as covered by never being measured, which
    is the one way to raise the floor while testing less."""
    source = set(_declared("tool", "coverage", "run", "source"))
    missing = sorted(_root_modules() - source)

    assert not missing, f"root modules outside [tool.coverage.run] source: {missing}"


def test_the_type_checker_sees_the_root_modules() -> None:
    """pyrightconfig.json lists includes by filename, with the same exhaustive-
    list problem: an unlisted module is simply not type-checked, and the gating
    step passes for the wrong reason."""
    import json
    import re

    raw = (ROOT / "pyrightconfig.json").read_text()
    # The config is JSONC — the header explains why each setting is what it is.
    config = as_object(cast(object, json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))))
    included = {Path(str(p)).stem for p in as_list(config.get("include"))}
    missing = sorted(_root_modules() - included)

    assert not missing, f"root modules outside pyrightconfig.json include: {missing}"
