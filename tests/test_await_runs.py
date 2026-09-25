"""eval.await_runs: when the nightly may merge its own reconciliation PR."""

from __future__ import annotations

import pytest
import requests

from eval import await_runs as A
from tests.stubs import const

LINT, EVALS = "Lint & unit tests", "MCP Evals"


def run(name: str, status: str = "completed", conclusion: str = "success") -> A.Run:
    return A.Run(name, status, conclusion)


def decide(*runs: A.Run) -> str | None:
    return A.verdict(runs, require=[LINT], settle=[EVALS])


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------
def test_lint_green_and_evals_skipped_may_merge() -> None:
    """The ordinary night: an unlabelled PR skips the evals."""
    assert decide(run(LINT), run(EVALS, conclusion="skipped")) == ""


def test_a_run_that_has_not_registered_yet_is_waited_for() -> None:
    """The race this exists for: a second after `opened`, nothing is listed."""
    assert decide() is None
    assert decide(run(LINT)) is None


def test_an_unfinished_run_is_waited_for() -> None:
    assert decide(run(LINT, status="in_progress", conclusion=""), run(EVALS, conclusion="skipped")) is None
    assert decide(run(LINT), run(EVALS, status="queued", conclusion="")) is None


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped"])
def test_a_required_run_that_did_not_pass_blocks_with_the_reason(conclusion: str) -> None:
    assert decide(run(LINT, conclusion=conclusion), run(EVALS, conclusion="skipped")) == (
        f"`{LINT}` concluded {conclusion}"
    )


def test_a_settled_run_may_conclude_anything() -> None:
    """The evals are not what the merge waits on them for — only their ref."""
    assert decide(run(LINT), run(EVALS, conclusion="failure")) == ""


def test_only_the_newest_run_of_a_workflow_counts() -> None:
    """Newest first, as the API lists them: a re-run that passed overrides the
    failure before it, and the other way round."""
    assert decide(run(LINT), run(LINT, conclusion="failure"), run(EVALS)) == ""
    assert decide(run(LINT, conclusion="failure"), run(LINT), run(EVALS)) == f"`{LINT}` concluded failure"


def test_unrelated_workflows_are_ignored() -> None:
    assert decide(run("Dependency Graph", conclusion="failure"), run(LINT), run(EVALS)) == ""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_polls_until_the_runs_finish() -> None:
    answers = [[], [run(LINT, status="in_progress", conclusion="")], [run(LINT), run(EVALS, conclusion="skipped")]]
    clock = Clock()

    code, reason = A.wait(
        lambda: answers.pop(0), [LINT], [EVALS], timeout=60, interval=10, sleep=clock.sleep, clock=clock
    )

    assert (code, reason) == (0, "")
    assert clock.now == 20


def test_wait_reports_a_failed_requirement_immediately() -> None:
    clock = Clock()

    code, reason = A.wait(
        const([run(LINT, conclusion="failure"), run(EVALS)]), [LINT], [EVALS], 60, 10, clock.sleep, clock
    )

    assert (code, reason) == (1, f"`{LINT}` concluded failure")
    assert clock.now == 0


def test_wait_gives_up_at_the_deadline_and_says_on_what() -> None:
    clock = Clock()

    code, reason = A.wait(const([]), [LINT], [EVALS], timeout=30, interval=10, sleep=clock.sleep, clock=clock)

    assert code == 2
    assert reason == f"`{LINT}`, `{EVALS}` had not finished on this commit after 30s"


# ---------------------------------------------------------------------------
# The API and the CLI
# ---------------------------------------------------------------------------
class Reply:
    def __init__(self, body: object) -> None:
        self.body: object = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self.body


def test_fetch_runs_asks_for_this_commits_pull_request_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> Reply:
        assert url == "https://api.github.com/repos/o/r/actions/runs" and timeout == 30
        assert headers["Authorization"] == "Bearer tok"
        seen.append(params)
        return Reply({"workflow_runs": [{"name": LINT, "status": "completed", "conclusion": None}]})

    monkeypatch.setattr(requests, "get", fake_get)

    assert A.fetch_runs("o/r", "abc", "tok") == [A.Run(LINT, "completed", "")]
    assert seen == [{"head_sha": "abc", "event": "pull_request", "per_page": "100"}]


def test_main_exits_with_the_verdict_and_prints_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setattr(A, "fetch_runs", const([run(LINT, conclusion="failure"), run(EVALS)]))

    code = A.main(["--sha", "abc", "--repo", "o/r", "--require", LINT, "--settle", EVALS])

    assert code == 1
    assert capsys.readouterr().out.strip() == f"`{LINT}` concluded failure"


def test_main_passes_quietly(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setattr(A, "fetch_runs", const([run(LINT), run(EVALS, conclusion="skipped")]))

    assert A.main(["--sha", "abc", "--repo", "o/r", "--require", LINT, "--settle", EVALS]) == 0
    assert capsys.readouterr().out == ""


def test_main_without_a_token_cannot_wait(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert A.main(["--sha", "abc", "--repo", "o/r"]) == 2
    assert "no GH_TOKEN" in capsys.readouterr().out
