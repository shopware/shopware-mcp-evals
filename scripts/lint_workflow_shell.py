#!/usr/bin/env python3
"""ShellCheck the shell that lives inside workflow `run:` blocks.

The ShellCheck step used to lint `functional/**/*.sh` and `scripts/**/*.sh`, which
is 116 lines across two files. The shell this repo actually runs is 866 lines
across 35 `run:` blocks in .github — so 88% of it was invisible to the shell
linter, and the lane setup, the eval gate and the store diagnosis were all in the
invisible part.

Worth being honest about the payoff: measured when this was written, those 866
lines were already clean at warning level, and none of the three shell mistakes
made in the store-diagnosis work were of a kind ShellCheck detects (a pipeline
masking an exit status in `if cmd | tee`, a grep pattern that matched by luck, and
`%0A` newline encoding). This is a regression guard on a currently-clean surface,
not a fix for a backlog.

Two details decide whether the output is usable at all, and both were found by
getting them wrong first:

  * `${{ ... }}` is not shell, so it is substituted with a bare `$GHA_EXPR`.
    Substituting a *literal* turns `[ "${{ inputs.x }}" = "y" ]` into a constant
    comparison and earns SC2050 on every one; substituting a *quoted* token yields
    `""$GHA_EXPR""` and earns SC2027 about quotes the workflow does not contain.
    Bare preserves the quoting the YAML actually has.
  * each block is prefixed with the shell options GitHub actually uses, so the
    findings match what runs. A composite action's `shell: bash` is `-eo pipefail`;
    a workflow `run:` defaults to `bash -e`.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

import yaml

from eval.result_schema import as_list, as_object

# Bare, so the quoting in the YAML is preserved exactly: `"${{ x }}"` becomes
# `"$GHA_EXPR"` and a bare `${{ x }}` stays bare — which then earns a legitimate
# SC2086 for an unquoted expansion, exactly as it should.
EXPRESSION = "$GHA_EXPR"
PREAMBLE = "#!/usr/bin/env bash\n"
# Only declared when a substitution actually happened. Declaring it unconditionally
# made every block WITHOUT a `${{ }}` report SC2034 "GHA_EXPR appears unused" — 35
# findings, all of them the linter's own scaffolding.
DECLARATION = "GHA_EXPR=x\n"


def run_blocks(path: Path) -> list[tuple[str, str]]:
    """Every `run:` block in a workflow or composite action, as (label, script).

    Both shapes: a workflow has `jobs.<id>.steps`, a composite action has
    `runs.steps`. Anything else contributes nothing rather than raising — this is
    a linter, and a file it cannot parse is the YAML parser's business.
    """
    document = as_object(cast(object, yaml.safe_load(path.read_text())))
    step_lists: list[tuple[str, list[object]]] = []

    for job_id, job in as_object(document.get("jobs")).items():
        step_lists.append((job_id, as_list(as_object(job).get("steps"))))
    composite = as_list(as_object(document.get("runs")).get("steps"))
    if composite:
        step_lists.append(("runs", composite))

    out: list[tuple[str, str]] = []
    for job_id, steps in step_lists:
        for index, raw in enumerate(steps):
            step = as_object(raw)
            script = step.get("run")
            if not isinstance(script, str):
                continue
            name = str(step.get("name") or step.get("id") or f"step-{index}")
            out.append((f"{path.name} / {job_id} / {name}", script))
    return out


def as_script(script: str, *, pipefail: bool) -> str:
    """One block, ready for ShellCheck, under the options GitHub runs it with."""
    options = "set -eo pipefail\n" if pipefail else "set -e\n"
    body = re.sub(r"\$\{\{[^}]*\}\}", EXPRESSION, script)
    declaration = DECLARATION if body != script else ""
    return PREAMBLE + declaration + options + body


def check(paths: list[Path], severity: str) -> int:
    """ShellCheck every block. Returns the number of files with findings."""
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            # A composite action's `shell: bash` is `-eo pipefail`; a workflow
            # `run:` is `bash -e`. Getting this wrong reports failures that cannot
            # happen, or misses ones that can.
            pipefail = path.name == "action.yml"
            for label, script in run_blocks(path):
                slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
                block = Path(tmp) / f"{slug}.sh"
                block.write_text(as_script(script, pipefail=pipefail))
                result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                    ["shellcheck", "-S", severity, "-f", "gcc", str(block)],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    failed += 1
                    print(f"\n== {label}")
                    # Report the label rather than the temp path, which tells the
                    # reader nothing about where to go and fix it.
                    for line in result.stdout.splitlines():
                        print("  " + line.replace(str(block), label))
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    # `style`, the strictest, because it is free: these blocks are clean at every
    # level today. It is also the only level that bites — the first version
    # defaulted to `warning`, and a planted `echo $unquoted` sailed through,
    # because SC2086 is info. A gate that passes a real bug is worse than none,
    # since it reads as coverage.
    parser.add_argument("--severity", default="style", choices=["error", "warning", "info", "style"])
    parser.add_argument("--root", default=".github")
    args = parser.parse_args()
    root = cast(str, args.root)
    severity = cast(str, args.severity)

    paths = sorted(Path(root).rglob("*.yml"))
    if not paths:
        print(f"No workflow YAML under {root}.")
        return 0

    blocks = sum(len(run_blocks(p)) for p in paths)
    lines = sum(len(s.splitlines()) for p in paths for _, s in run_blocks(p))
    print(f"ShellCheck over {blocks} run: blocks ({lines} lines) in {len(paths)} files, severity {severity}")

    failed = check(paths, severity)
    if failed:
        print(f"\n{failed} block(s) with findings.")
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
