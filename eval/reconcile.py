#!/usr/bin/env python3
"""Whether the nightly may merge its own reconciliation PR.

Only one kind of reconciliation is safe to merge unattended: the one that moves
`shopware.sha` and nothing else, on a night that tested that exact commit and
passed. Everything else — a tool added, removed or reworded — needs a human,
because it decides which fixtures grade and which skip.

"No catalogue drift" is not enough by itself, for two reasons learned the hard
way:

  * #47 merged under that headline while renaming every UCP tool, because the
    report only covered one of the two snapshots. So the rule reads the staged
    files, not the headline: if a snapshot is in the diff, a human reviews it.
  * shopware/shopware#20600 changed no description at all and took the whole
    admin lane down (an unset allowlist started granting nothing). Its catalogue
    would have read "no drift". So the night's own gating jobs must have passed
    on the commit being pinned — otherwise the pin moves every PR onto a Shopware
    that is already red.

Pure decision in `blockers()`; the CLI only loads files and prints. Exit 0 means
"merge it", exit 1 means "leave it for a human", with the reasons on stdout for
the PR body.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import cast

from eval.drift import collapsed, is_significant, load, summarise
from eval.result_schema import Snapshot

# What an unattended merge may change. The lint budget only ever tightens here
# (`toollint --tighten-budget`), and it follows the snapshot, so with the snapshot
# unchanged it cannot move either; it is listed so a harmless re-stamp does not
# block the merge.
MERGEABLE_FILES = frozenset({"shopware.sha", "tool-history/lint-budget.json"})

# What must have succeeded on the commit being pinned. `static` and `admin-eval`
# are the gates; `store-snapshot` is the step that measures the Store catalogue.
# It runs continue-on-error and is skipped without the plugin, and in both cases
# the committed store.json is still on disk — so without this entry the Store
# comparison would read the baseline against itself and call it "no drift". The
# Store EVAL is advisory everywhere else, so it gets no veto here.
REQUIRED = ("static", "admin-eval", "store-snapshot")


def blockers(
    staged: Sequence[str],
    snapshots: Sequence[tuple[str, Snapshot, Snapshot]],
    job_results: Mapping[str, str],
) -> list[str]:
    """Every reason not to merge. Empty means the PR may merge itself."""
    reasons: list[str] = []

    extra = sorted(set(staged) - MERGEABLE_FILES)
    if extra:
        reasons.append(f"it changes {', '.join(f'`{f}`' for f in extra)}, which needs a review")
    if "shopware.sha" not in staged:
        reasons.append("it does not move `shopware.sha`, so there is nothing to pin")

    # Belt and braces: with the snapshot files out of the diff these cannot fire,
    # but the rule should not depend on the file check alone being right.
    for label, old, new in snapshots:
        if collapsed(old, new):
            reasons.append(f"the {label} catalogue collapsed")
        elif is_significant(summarise(old, new)):
            reasons.append(f"the {label} catalogue drifted")

    for job in REQUIRED:
        result = job_results.get(job, "missing")
        if result != "success":
            reasons.append(f"`{job}` did not pass on this Shopware commit ({result})")

    return reasons


def _job_results(pairs: Sequence[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for pair in pairs:
        name, _, result = pair.partition("=")
        results[name] = result
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staged", nargs="*", default=[], help="files the reconciliation commit changes")
    parser.add_argument(
        "--snapshot",
        nargs=3,
        action="append",
        default=[],
        metavar=("LABEL", "OLD", "NEW"),
        help="a baseline and a fresh snapshot to compare; repeatable",
    )
    parser.add_argument("--job", action="append", default=[], help="NAME=RESULT of a job this night ran; repeatable")
    args = parser.parse_args(argv)

    snapshots: list[tuple[str, Snapshot, Snapshot]] = []
    unreadable: list[str] = []
    for label, old_path, new_path in cast(list[list[str]], args.snapshot):
        try:
            snapshots.append((label, load(old_path), load(new_path)))
        except (OSError, json.JSONDecodeError):
            unreadable.append(f"the {label} snapshots could not be compared")

    reasons = unreadable + blockers(cast(list[str], args.staged), snapshots, _job_results(cast(list[str], args.job)))
    if reasons:
        print("**Left for review:** " + "; ".join(reasons) + ".")
        return 1
    print(
        "**Merged automatically:** only `shopware.sha` moved, both catalogues were measured "
        "tonight and neither drifted, and `static` and `admin-eval` passed on this Shopware commit."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
