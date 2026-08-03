"""The functional runner's CLI entry point.

`main()` is what CI invokes, and it owns four decisions that only surface at the
edges: which credentials each endpoint needs, what to do when the session cannot
open, that a mid-suite transport failure still produces a summary and a report,
and that the exit code follows the checks rather than the absence of a crash.
"""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import requests

from eval.result_schema import JsonObject, as_list, as_object
from functional import runner as R
from functional.reporting import Reporter
from tests.stubs import const, never, raiser


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Real credentials, a stub session, and reports written to tmp_path."""
    for name in ("SW_BASE_URL", "SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "SW_SC_ACCESS_KEY"):
        monkeypatch.setattr(R, name, "set")
    monkeypatch.setattr(R, "BASE", tmp_path)
    monkeypatch.setattr(R, "mcp_init", const(("session-1", "")))
    monkeypatch.setattr(R, "run_admin", const(None))
    monkeypatch.setattr(R, "run_store", const(None))


def run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["functional.runner", *argv])
    return R.main()


def report_of(tmp_path: Path) -> JsonObject:
    files = list((tmp_path / "results").glob("functional-*.json"))
    assert len(files) == 1, files
    return as_object(cast(object, json.loads(files[0].read_text())))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_admin_run_requires_the_integration_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "SW_SECRET_ACCESS_KEY", "")

    with pytest.raises(SystemExit, match="SW_SECRET_ACCESS_KEY is required"):
        run(monkeypatch, "--endpoint", "admin")


def test_store_run_requires_the_sales_channel_key_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requiring the admin pair for a store run would block a correctly
    configured one; requiring neither would fail deep inside the suite."""
    monkeypatch.setattr(R, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(R, "SW_SECRET_ACCESS_KEY", "")

    assert run(monkeypatch, "--endpoint", "store") == 0


def test_store_run_without_the_sales_channel_key_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "SW_SC_ACCESS_KEY", "")

    with pytest.raises(SystemExit, match="sales-channel access key"):
        run(monkeypatch, "--endpoint", "store")


def test_a_missing_base_url_stops_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "SW_BASE_URL", "")
    monkeypatch.setattr(R, "mcp_init", never("must not connect"))

    with pytest.raises(SystemExit, match="SW_BASE_URL is required"):
        run(monkeypatch)


# ---------------------------------------------------------------------------
# Session failures
# ---------------------------------------------------------------------------
def test_a_refused_connection_is_reported_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(R, "mcp_init", raiser(requests.exceptions.ConnectionError("Connection refused")))

    assert run(monkeypatch) == 1
    assert "Failed to initialize MCP session: Connection refused" in capsys.readouterr().out


def test_an_empty_session_id_is_treated_as_a_credential_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(R, "mcp_init", const(("", "")))

    assert run(monkeypatch) == 1
    assert "Check credentials" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dispatch and flags
# ---------------------------------------------------------------------------
def test_the_endpoint_flag_chooses_which_suite_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def note(name: str) -> Callable[..., None]:
        def stub(*_args: object, **_kwargs: object) -> None:
            called.append(name)

        return stub

    monkeypatch.setattr(R, "run_admin", note("admin"))
    monkeypatch.setattr(R, "run_store", note("store"))

    run(monkeypatch, "--endpoint", "admin")
    run(monkeypatch, "--endpoint", "store")

    assert called == ["admin", "store"]


def test_the_skip_flags_reach_the_admin_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: JsonObject = {}

    def capture(_rep: object, _ep: object, args: argparse.Namespace, _session: str) -> None:
        seen.update(vars(args))

    monkeypatch.setattr(R, "run_admin", capture)

    run(monkeypatch, "--skip-media-upload", "--skip-dev-tools")

    assert seen["skip_media_upload"] is True and seen["skip_dev_tools"] is True


def test_the_skip_flags_default_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: JsonObject = {}

    def capture(_rep: object, _ep: object, args: argparse.Namespace, _session: str) -> None:
        seen.update(vars(args))

    monkeypatch.setattr(R, "run_admin", capture)

    run(monkeypatch)

    assert seen["skip_media_upload"] is False and seen["skip_dev_tools"] is False


# ---------------------------------------------------------------------------
# Aborting cleanly
# ---------------------------------------------------------------------------
def test_a_transport_failure_mid_suite_still_writes_a_summary_and_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The suite has to survive the server dying halfway: without this the run
    crashes and CI reports no results at all rather than results plus a cause."""

    monkeypatch.setattr(R, "run_admin", raiser(requests.exceptions.HTTPError("500 Server Error")))

    code = run(monkeypatch)
    out = capsys.readouterr().out

    assert code == 1
    assert "suite aborted early: 500 Server Error" in out
    assert "Results:" in out, "a summary must still be printed"
    assert any(as_object(c).get("status") == "fail" for c in as_list(report_of(tmp_path)["tools"]))


def test_a_non_transport_exception_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only RequestException is an expected abort. A bug in a check should not be
    reported as a clean run with one failure."""

    monkeypatch.setattr(R, "run_admin", raiser(KeyError("typo in a payload key")))

    with pytest.raises(KeyError):
        run(monkeypatch)


# ---------------------------------------------------------------------------
# Report and exit code
# ---------------------------------------------------------------------------
def test_a_clean_run_exits_zero_and_writes_a_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert run(monkeypatch) == 0

    report = report_of(tmp_path)
    assert report["fail"] == 0
    assert report["server"] == "set"


def test_the_exit_code_follows_the_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_one(rep: Reporter, *_args: object, **_kwargs: object) -> None:
        rep.check_fail("a check", "boom")

    monkeypatch.setattr(R, "run_admin", fail_one)

    assert run(monkeypatch) == 1
    assert report_of(tmp_path)["fail"] == 1


def test_the_report_filename_names_the_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run(monkeypatch, "--endpoint", "store")

    assert list((tmp_path / "results").glob("functional-store-*.json"))
