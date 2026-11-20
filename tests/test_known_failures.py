"""Known failures: told apart from new ones, and reported with what they wait for.

The registry exists so a reader of a CI run can tell an expected failure from a
new one without opening the log. Each test here pins one half of that: the
match is narrow enough that a new failure on a known check still reads as new,
the exit code ignores only what is explained, and the annotations say which is
which.
"""

import json
from pathlib import Path
from typing import cast

import pytest

from eval.result_schema import JsonObject, as_object
from functional import journeys
from functional.known_failures import KNOWN_FAILURES, KnownFailure, match, match_label
from functional.reporting import Reporter, workflow_command

FUNCTIONAL = Path(__file__).resolve().parent.parent / "functional"

ENTRY = KnownFailure(
    label="a check",
    error="the known cause",
    issue="org/repo#1",
    url="https://example.invalid/org/repo/issues/1",
    waiting_for="a fix upstream",
    why="Explained.",
)


def reporter() -> Reporter:
    return Reporter("srv", color=False, known=(ENTRY,))


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------
def test_second_order_entry_names_the_check_journeys_reports() -> None:
    """Spelled out rather than imported, so this is what keeps them equal."""
    assert any(entry.label == journeys.SECOND_ORDER_CHECK for entry in KNOWN_FAILURES)


def test_every_entry_names_a_check_the_suite_still_has() -> None:
    """An entry for a check that was renamed or removed can never match or
    recover, so it would sit in the registry describing nothing."""
    sources = "\n".join(path.read_text() for path in FUNCTIONAL.glob("*.py") if path.name != "known_failures.py")
    for entry in KNOWN_FAILURES:
        assert f'"{entry.label}"' in sources, entry.label


def test_every_entry_says_what_it_waits_for() -> None:
    for entry in KNOWN_FAILURES:
        assert entry.error and entry.waiting_for and entry.why
        # An issue or the pull request that fixes it.
        assert entry.url.startswith("https://github.com/")
        assert entry.issue in entry.url.replace("/issues/", "#").replace("/pull/", "#")


def test_match_needs_both_the_label_and_the_error() -> None:
    known = (ENTRY,)
    assert match("a check", "boom: the known cause", known) is ENTRY
    assert match("a check", "a different cause", known) is None, "a new cause on a known check is new"
    assert match("another check", "the known cause", known) is None


def test_the_label_matches_as_a_prefix() -> None:
    """The second order's pass label appends the order ids."""
    assert match_label("a check (1234… after 5678…)", (ENTRY,)) is ENTRY
    assert match_label("a different check", (ENTRY,)) is None


def test_the_entry_describes_itself_with_its_issue() -> None:
    text = ENTRY.describe()
    assert "org/repo#1" in text and ENTRY.url in text and "a fix upstream" in text


# ---------------------------------------------------------------------------
# The reporter
# ---------------------------------------------------------------------------
def test_a_known_failure_does_not_fail_the_run() -> None:
    rep = reporter()
    rep.check_pass("fine")
    rep.check_fail("a check", "the known cause")

    assert (rep.failed, rep.expected_failed, rep.unexpected_failed) == (1, 1, 0)
    assert rep.exit_code == 0
    assert rep.records[-1] == {
        "tool": "check",
        "label": "a check",
        "status": "fail",
        "error": "the known cause",
        "known_issue": "org/repo#1",
        "principal": "admin",
    }


def test_an_unexpected_failure_still_fails_it() -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")
    rep.tool_fail("shopware-x", "x works", "boom")

    assert rep.exit_code == 1
    assert "known_issue" not in rep.records[-1]


def test_the_printed_line_marks_the_failure_as_expected(capsys: pytest.CaptureFixture[str]) -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")
    rep.summary()

    out = capsys.readouterr().out
    assert "FAIL (expected, org/repo#1) a check: the known cause" in out
    assert "1 failed (1 expected)" in out


def test_a_local_run_says_a_known_failure_passed(capsys: pytest.CaptureFixture[str]) -> None:
    """The warning annotation only reaches GitHub, so the summary says it too."""
    rep = reporter()
    rep.check_pass("a check")
    rep.summary()

    assert "Known failure passed: a check. Was waiting on org/repo#1" in capsys.readouterr().out


def test_the_report_counts_expected_failures(tmp_path: Path) -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")
    out = tmp_path / "r.json"
    rep.write_report(out)

    report = as_object(cast(object, json.loads(out.read_text())))
    assert (report["fail"], report["expected_fail"]) == (1, 1)


def test_the_report_omits_the_count_when_there_is_none(tmp_path: Path) -> None:
    rep = reporter()
    rep.check_fail("other", "boom")
    out = tmp_path / "r.json"
    rep.write_report(out)

    assert "expected_fail" not in cast(JsonObject, json.loads(out.read_text()))


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------
def test_a_clean_run_posts_nothing() -> None:
    rep = reporter()
    rep.check_pass("fine")
    assert rep.annotations("Suite") == []


def test_an_unexpected_failure_is_an_error_naming_the_check() -> None:
    rep = reporter()
    rep.tool_fail("shopware-x", "x works", "boom")

    [line] = rep.annotations("Suite")
    assert line.startswith("::error title=Suite::")
    assert "1 unexpected failure(s)" in line and "functional/known_failures.py" in line
    assert "The job fails on it." in line
    assert "FAIL x works: boom" in line


def test_on_an_advisory_suite_it_is_a_warning_that_says_the_job_stays_green() -> None:
    rep = reporter()
    rep.tool_fail("shopware-x", "x works", "boom")

    [line] = rep.annotations("Suite", advisory=True)
    assert line.startswith("::warning title=Suite::")
    assert "the job stays green" in line


def test_a_known_failure_is_a_notice_naming_its_issue() -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")

    [line] = rep.annotations("Suite")
    assert line.startswith("::notice title=Suite::")
    assert "1 expected failure(s)" in line and "do not fail the job" in line
    assert "FAIL a check: the known cause" in line
    assert ENTRY.url in line and "waiting for a fix upstream" in line


def test_the_issue_is_described_once_however_often_it_fails() -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")
    rep.check_fail("a check", "again the known cause")

    [line] = rep.annotations("Suite")
    assert line.count(ENTRY.url) == 1


def test_new_and_known_failures_get_one_annotation_each() -> None:
    rep = reporter()
    rep.check_fail("a check", "the known cause")
    rep.tool_fail("shopware-x", "x works", "boom")

    levels = [line.split(" ", 1)[0] for line in rep.annotations("Suite", advisory=True)]
    assert levels == ["::warning", "::notice"]


def test_a_known_check_that_passes_asks_for_the_entry_to_go() -> None:
    rep = reporter()
    rep.check_pass("a check (after the fix)")

    [line] = rep.annotations("Suite")
    assert line.startswith("::warning title=Suite::")
    assert "1 known failure(s) passed" in line and "delete the entry" in line
    assert ENTRY.url in line


def test_skips_are_posted_only_when_asked() -> None:
    rep = reporter()
    rep.skip("bare skip")
    rep.check_skip("a structural check", "waiting for the server")
    rep.tool_skip("shopware-x", "x works", "not registered")

    assert rep.annotations("Suite") == []
    [line] = rep.annotations("Suite", skips=True)
    assert line.startswith("::notice title=Suite::")
    assert "SKIP bare skip" in line and "SKIP a structural check: waiting for the server" in line
    assert "3 skipped" in line


def test_a_run_without_skips_says_so_when_asked() -> None:
    [line] = reporter().annotations("Suite", skips=True)
    assert "No skipped checks." in line


def test_long_lists_are_cut_and_say_how_many_are_left() -> None:
    rep = reporter()
    for n in range(10):
        rep.tool_fail("shopware-x", f"check {n}", "boom")
        rep.skip(f"skip {n}")

    error, skips = rep.annotations("Suite", skips=True)
    assert "check 7" in error and "check 8" not in error and "and 2 more" in error
    assert "skip 7" in skips and "skip 8" not in skips and "and 2 more" in skips


def test_a_long_error_is_truncated() -> None:
    rep = reporter()
    rep.tool_fail("shopware-x", "x works", "e" * 1000)

    [line] = rep.annotations("Suite")
    assert "e" * 300 + "..." in line and "e" * 301 not in line


def test_the_command_escapes_what_the_runner_would_unescape() -> None:
    """A newline or a comma unescaped ends the message or the title early."""
    line = workflow_command("notice", "Store functional suite, MCP 2025-06-18", ["100% done", "next: line"])
    assert line == "::notice title=Store functional suite%2C MCP 2025-06-18::100%25 done%0Anext: line"
