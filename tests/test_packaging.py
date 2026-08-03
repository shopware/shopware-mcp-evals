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

ROOT = Path(__file__).resolve().parents[1]

# Root-level modules that are deliberately not importable library code.
NOT_LIBRARY = set()


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _root_modules():
    return {p.stem for p in ROOT.glob("*.py") if not p.stem.startswith("_")} - NOT_LIBRARY


def test_every_root_module_is_declared_for_the_install():
    declared = set(_pyproject()["tool"]["setuptools"]["py-modules"])
    missing = sorted(_root_modules() - declared)

    assert not missing, (
        f"top-level modules absent from [tool.setuptools] py-modules in pyproject.toml: {missing}. "
        "They import fine from the repo root and are missing from a fresh `pip install -e .`, "
        "so this only fails in CI unless it fails here."
    )


def test_no_declared_module_has_been_deleted():
    """The other direction: a stale name makes the build fail outright rather
    than silently, but the message names setuptools internals rather than the
    file somebody removed."""
    declared = set(_pyproject()["tool"]["setuptools"]["py-modules"])
    stale = sorted(declared - _root_modules())

    assert not stale, f"py-modules names modules that no longer exist: {stale}"


def test_the_coverage_source_list_covers_the_root_modules():
    """A module missing here reports as covered by never being measured, which
    is the one way to raise the floor while testing less."""
    source = set(_pyproject()["tool"]["coverage"]["run"]["source"])
    missing = sorted(_root_modules() - source)

    assert not missing, f"root modules outside [tool.coverage.run] source: {missing}"


def test_the_type_checker_sees_the_root_modules():
    """pyrightconfig.json lists includes by filename, with the same exhaustive-
    list problem: an unlisted module is simply not type-checked, and the gating
    step passes for the wrong reason."""
    import json
    import re

    raw = (ROOT / "pyrightconfig.json").read_text()
    # The config is JSONC — the header explains why each setting is what it is.
    config = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE))
    included = {Path(p).stem for p in config["include"]}
    missing = sorted(_root_modules() - included)

    assert not missing, f"root modules outside pyrightconfig.json include: {missing}"
