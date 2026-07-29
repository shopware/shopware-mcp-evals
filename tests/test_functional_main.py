"""The functional runner's CLI entry point.

`main()` is what CI invokes, and it owns four decisions that only surface at the
edges: which credentials each endpoint needs, what to do when the session cannot
open, that a mid-suite transport failure still produces a summary and a report,
and that the exit code follows the checks rather than the absence of a crash.
"""

import json

import pytest
import requests

from functional import runner as R


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Real credentials, a stub session, and reports written to tmp_path."""
    for name in ("SW_BASE_URL", "SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "SW_SC_ACCESS_KEY"):
        monkeypatch.setattr(R, name, "set")
    monkeypatch.setattr(R, "BASE", tmp_path)
    monkeypatch.setattr(R, "mcp_init", lambda endpoint=None: ("session-1", ""))
    monkeypatch.setattr(R, "run_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(R, "run_store", lambda *_a, **_k: None)


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["functional.runner", *argv])
    return R.main()


def report_of(tmp_path):
    files = list((tmp_path / "results").glob("functional-*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_admin_run_requires_the_integration_pair(monkeypatch):
    monkeypatch.setattr(R, "SW_SECRET_ACCESS_KEY", "")

    with pytest.raises(SystemExit, match="SW_SECRET_ACCESS_KEY is required"):
        run(monkeypatch, "--endpoint", "admin")


def test_store_run_requires_the_sales_channel_key_instead(monkeypatch):
    """Requiring the admin pair for a store run would block a correctly
    configured one; requiring neither would fail deep inside the suite."""
    monkeypatch.setattr(R, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(R, "SW_SECRET_ACCESS_KEY", "")

    assert run(monkeypatch, "--endpoint", "store") == 0


def test_store_run_without_the_sales_channel_key_exits(monkeypatch):
    monkeypatch.setattr(R, "SW_SC_ACCESS_KEY", "")

    with pytest.raises(SystemExit, match="sales-channel access key"):
        run(monkeypatch, "--endpoint", "store")


def test_a_missing_base_url_stops_before_any_request(monkeypatch):
    monkeypatch.setattr(R, "SW_BASE_URL", "")
    monkeypatch.setattr(R, "mcp_init", lambda **_k: pytest.fail("must not connect"))

    with pytest.raises(SystemExit, match="SW_BASE_URL is required"):
        run(monkeypatch)


# ---------------------------------------------------------------------------
# Session failures
# ---------------------------------------------------------------------------
def test_a_refused_connection_is_reported_and_exits_one(monkeypatch, capsys):
    def boom(endpoint=None):
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr(R, "mcp_init", boom)

    assert run(monkeypatch) == 1
    assert "Failed to initialize MCP session: Connection refused" in capsys.readouterr().out


def test_an_empty_session_id_is_treated_as_a_credential_problem(monkeypatch, capsys):
    monkeypatch.setattr(R, "mcp_init", lambda endpoint=None: ("", ""))

    assert run(monkeypatch) == 1
    assert "Check credentials" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dispatch and flags
# ---------------------------------------------------------------------------
def test_the_endpoint_flag_chooses_which_suite_runs(monkeypatch):
    called = []
    monkeypatch.setattr(R, "run_admin", lambda *_a, **_k: called.append("admin"))
    monkeypatch.setattr(R, "run_store", lambda *_a, **_k: called.append("store"))

    run(monkeypatch, "--endpoint", "admin")
    run(monkeypatch, "--endpoint", "store")

    assert called == ["admin", "store"]


def test_the_skip_flags_reach_the_admin_suite(monkeypatch):
    seen = {}
    monkeypatch.setattr(R, "run_admin", lambda _rep, _ep, args, _s: seen.update(vars(args)))

    run(monkeypatch, "--skip-media-upload", "--skip-dev-tools")

    assert seen["skip_media_upload"] is True and seen["skip_dev_tools"] is True


def test_the_skip_flags_default_to_off(monkeypatch):
    seen = {}
    monkeypatch.setattr(R, "run_admin", lambda _rep, _ep, args, _s: seen.update(vars(args)))

    run(monkeypatch)

    assert seen["skip_media_upload"] is False and seen["skip_dev_tools"] is False


# ---------------------------------------------------------------------------
# Aborting cleanly
# ---------------------------------------------------------------------------
def test_a_transport_failure_mid_suite_still_writes_a_summary_and_report(monkeypatch, tmp_path, capsys):
    """The suite has to survive the server dying halfway: without this the run
    crashes and CI reports no results at all rather than results plus a cause."""

    def dies(*_a, **_k):
        raise requests.exceptions.HTTPError("500 Server Error")

    monkeypatch.setattr(R, "run_admin", dies)

    code = run(monkeypatch)
    out = capsys.readouterr().out

    assert code == 1
    assert "suite aborted early: 500 Server Error" in out
    assert "Results:" in out, "a summary must still be printed"
    assert any(c["status"] == "fail" for c in report_of(tmp_path)["tools"])


def test_a_non_transport_exception_is_not_swallowed(monkeypatch):
    """Only RequestException is an expected abort. A bug in a check should not be
    reported as a clean run with one failure."""

    def bug(*_a, **_k):
        raise KeyError("typo in a payload key")

    monkeypatch.setattr(R, "run_admin", bug)

    with pytest.raises(KeyError):
        run(monkeypatch)


# ---------------------------------------------------------------------------
# Report and exit code
# ---------------------------------------------------------------------------
def test_a_clean_run_exits_zero_and_writes_a_report(monkeypatch, tmp_path):
    assert run(monkeypatch) == 0

    report = report_of(tmp_path)
    assert report["fail"] == 0
    assert report["server"] == "set"


def test_the_exit_code_follows_the_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "run_admin", lambda rep, *_a, **_k: rep.check_fail("a check", "boom"))

    assert run(monkeypatch) == 1
    assert report_of(tmp_path)["fail"] == 1


def test_the_report_filename_names_the_endpoint(monkeypatch, tmp_path):
    run(monkeypatch, "--endpoint", "store")

    assert list((tmp_path / "results").glob("functional-store-*.json"))
