"""Wait for a commit's pull_request runs before the nightly merges its own PR.

The reconciliation PR used to be merged one second after it was opened. The
runs its `opened` event had just queued then lost their ref and failed at
startup with no jobs — a red "MCP Evals" and a red "Lint & unit tests" after
every auto-merge. That is noise, and it also meant the pin moved without lint
having run on the commit it pins.

So the merge waits. `--require` names a workflow that must pass (lint);
`--settle` names one that only has to finish, whatever its conclusion (the
evals, which skip on an unlabelled PR). Exit 0: merge. Exit 1: a required run
did not pass. Exit 2: something was still running at the deadline. Both
non-zero cases print the reason, which goes into the PR comment.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

import requests

from eval.result_schema import as_list, as_object


@dataclass(frozen=True)
class Run:
    name: str
    status: str
    conclusion: str


def verdict(runs: Sequence[Run], require: Sequence[str], settle: Sequence[str]) -> str | None:
    """None while anything named is missing or unfinished; "" when the merge may
    go ahead; otherwise why not. One commit can have several runs of a workflow
    (a re-run, a label added), and only the newest counts; the API lists newest
    first."""
    latest: dict[str, Run] = {}
    for run in runs:
        _ = latest.setdefault(run.name, run)
    for name in (*require, *settle):
        run = latest.get(name)
        if run is None or run.status != "completed":
            return None
    failed = [_why(name, latest[name].conclusion) for name in require if latest[name].conclusion != "success"]
    return "; ".join(failed)


def _why(name: str, conclusion: str) -> str:
    """`action_required` is not a failure: the run never started, because GitHub
    wants a maintainer to approve it — which is what a PR pushed as
    github-actions[bot] gets. Saying "concluded action_required" sent #77's
    reader looking for a broken lint."""
    if conclusion == "action_required":
        return (
            f"`{name}` is waiting for a maintainer to approve its run, which GitHub requires when "
            "github-actions[bot] pushes the PR (was the octo-sts token missing?)"
        )
    return f"`{name}` concluded {conclusion}"


def fetch_runs(repo: str, sha: str, token: str) -> list[Run]:
    """pull_request runs for this commit, newest first (the API's order)."""
    reply = requests.get(
        f"https://api.github.com/repos/{repo}/actions/runs",
        params={"head_sha": sha, "event": "pull_request", "per_page": "100"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    reply.raise_for_status()
    runs: list[Run] = []
    for item in as_list(as_object(cast(object, reply.json())).get("workflow_runs")):
        run = as_object(item)
        runs.append(Run(str(run.get("name", "")), str(run.get("status", "")), str(run.get("conclusion") or "")))
    return runs


def wait(
    fetch: Callable[[], list[Run]],
    require: Sequence[str],
    settle: Sequence[str],
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, str]:
    deadline = clock() + timeout
    while True:
        runs = fetch()
        result = verdict(runs, require, settle)
        if result is not None:
            return (0, "") if result == "" else (1, result)
        if clock() >= deadline:
            return 2, _unfinished(runs, (*require, *settle), timeout)
        sleep(interval)


def _unfinished(runs: Sequence[Run], names: Sequence[str], timeout: float) -> str:
    """What was still missing at the deadline, split by cause.

    A run that never registered and one that is still going want different
    fixes: the first means the PR's events started no workflow at all (a PR
    opened with a token whose events GitHub does not act on would look exactly
    like this), the second only a slow runner. One sentence for both would send
    whoever reads the PR comment after the wrong one.
    """
    seen = {run.name for run in runs}
    never = [f"`{n}`" for n in names if n not in seen]
    running = [f"`{n}`" for n in names if n in seen]
    parts: list[str] = []
    if never:
        parts.append(f"{', '.join(never)} never started a pull_request run on this commit")
    if running:
        parts.append(f"{', '.join(running)} had not finished")
    return f"{'; '.join(parts)} after {int(timeout)}s"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha", required=True, help="the PR's head commit")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--require", action="append", default=[], help="workflow that must succeed; repeatable")
    parser.add_argument("--settle", action="append", default=[], help="workflow that only has to finish; repeatable")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("no GH_TOKEN or GITHUB_TOKEN to read the runs with")
        return 2
    repo, sha = cast(str, args.repo), cast(str, args.sha)
    code, reason = wait(
        lambda: fetch_runs(repo, sha, token),
        cast(list[str], args.require),
        cast(list[str], args.settle),
        cast(float, args.timeout),
        cast(float, args.interval),
    )
    if reason:
        print(reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
