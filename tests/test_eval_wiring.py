"""The configuration and report-assembly steps of an eval run.

These used to be statements inside a 294-line main(), reachable only by running
the whole suite against a live server and a paid provider. Each one is now a
function that either returns a value or raises ConfigError, so the failure modes
that matter — a typo'd --modes, a missing key, a filter that matches nothing —
are checked here instead of discovered in CI.
"""

import json

import pytest
import yaml

from eval import runner as E


def args(**over):
    """An argparse.Namespace as build_parser() would produce it, defaults included."""
    return E.build_parser().parse_args([f"--{k.replace('_', '-')}={v}" for k, v in over.items()])


# ---------------------------------------------------------------------------
# parse_modes
# ---------------------------------------------------------------------------
def test_discovery_is_the_only_mode():
    assert E.parse_modes("discovery") == ["discovery"]


def test_whitespace_and_trailing_commas_are_tolerated():
    assert E.parse_modes(" discovery , ") == ["discovery"]


def test_baseline_is_rejected_with_an_explanation():
    """It was a real mode until it turned out to be measuring its own grading, so
    the error has to say so rather than read as a typo."""
    with pytest.raises(E.ConfigError, match="baseline mode was removed"):
        E.parse_modes("baseline,discovery")


def test_a_typod_mode_is_rejected_by_name():
    """Silently running zero modes would report a vacuous PASS."""
    with pytest.raises(E.ConfigError, match="discovry"):
        E.parse_modes("discovry")


def test_an_empty_mode_list_is_rejected():
    with pytest.raises(E.ConfigError):
        E.parse_modes(",,")


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------
def test_explicit_model_wins_over_everything(monkeypatch):
    monkeypatch.setenv("EVAL_MODEL", "from-env")

    assert E.resolve_model("openai", "from-flag") == "from-flag"


def test_env_model_wins_over_the_provider_default(monkeypatch):
    monkeypatch.setenv("EVAL_MODEL", "from-env")

    assert E.resolve_model("openai", None) == "from-env"


def test_provider_default_is_the_fallback(monkeypatch):
    monkeypatch.delenv("EVAL_MODEL", raising=False)

    assert E.resolve_model("openai", None) == E.PROVIDER_DEFAULTS["openai"]


# ---------------------------------------------------------------------------
# require_credentials
# ---------------------------------------------------------------------------
def test_admin_run_needs_the_integration_keys(monkeypatch):
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(E, "ANTHROPIC_API_KEY", "k")

    with pytest.raises(E.ConfigError, match="SW_ACCESS_KEY"):
        E.require_credentials("anthropic", "admin")


def test_store_run_needs_the_sales_channel_key_not_the_integration_one(monkeypatch):
    """The store endpoint authenticates with a sales-channel key; requiring the
    admin pair there would block a perfectly configured store run."""
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "")
    monkeypatch.setattr(E, "SW_SECRET_ACCESS_KEY", "")
    monkeypatch.setattr(E, "SW_SC_ACCESS_KEY", "sc")
    monkeypatch.setattr(E, "OPENAI_API_KEY", "k")

    assert E.require_credentials("openai", "store") == ("OPENAI_API_KEY", "k")


def test_every_missing_variable_is_named_at_once(monkeypatch):
    """Reporting them one per run means one re-run per missing key."""
    for name in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(E, name, "")

    with pytest.raises(E.ConfigError) as exc:
        E.require_credentials("openai", "admin")

    assert all(n in str(exc.value) for n in ("SW_ACCESS_KEY", "SW_SECRET_ACCESS_KEY", "OPENAI_API_KEY"))


def test_github_provider_uses_the_workflow_token(monkeypatch):
    monkeypatch.setattr(E, "GITHUB_TOKEN", "ghs_x")
    monkeypatch.setattr(E, "SW_ACCESS_KEY", "a")
    monkeypatch.setattr(E, "SW_SECRET_ACCESS_KEY", "b")

    assert E.require_credentials("github", "admin") == ("GITHUB_TOKEN", "ghs_x")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def test_store_endpoint_picks_the_store_fixture_file():
    assert E.fixtures_path_for("store", None).name == "fixtures_store.yaml"
    assert E.fixtures_path_for("admin", None).name == "fixtures.yaml"


def test_an_explicit_fixtures_path_overrides_the_endpoint_default():
    assert E.fixtures_path_for("store", "/tmp/other.yaml").name == "other.yaml"


def fixture_file(tmp_path, fixtures):
    p = tmp_path / "f.yaml"
    p.write_text(yaml.safe_dump({"fixtures": fixtures}))
    return p


def test_category_and_id_filters_compose(tmp_path):
    p = fixture_file(
        tmp_path,
        [
            {"id": "a", "category": "meta", "expected_tool": "t"},
            {"id": "b", "category": "meta", "expected_tool": "t"},
            {"id": "c", "category": "chain", "expected_tool": "t"},
        ],
    )

    assert [f["id"] for f in E.load_fixtures(p, category="meta")] == ["a", "b"]
    assert [f["id"] for f in E.load_fixtures(p, fixture_id="c")] == ["c"]
    assert E.load_fixtures(p, category="meta", fixture_id="a")[0]["id"] == "a"


def test_a_filter_that_matches_nothing_is_an_error_not_an_empty_pass(tmp_path):
    """An empty fixture set would otherwise score 0/0 and gate as PASS."""
    p = fixture_file(tmp_path, [{"id": "a", "category": "meta", "expected_tool": "t"}])

    with pytest.raises(E.ConfigError, match="No fixtures matched"):
        E.load_fixtures(p, fixture_id="nope")


def test_the_real_fixture_files_load_through_this_path():
    """Guards the packaged-data path: fixtures live beside the runner, and an
    install that dropped the YAML would only surface here."""
    for endpoint, expected in (("admin", 90), ("store", 42)):
        fixtures = E.load_fixtures(E.fixtures_path_for(endpoint, None))
        assert len(fixtures) >= expected // 2
        # Negative fixtures name no tool — the flag is what identifies them.
        assert all("expected_tool" in f or f.get("expect_no_tool") for f in fixtures)


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------
def result(fid, passed=True, tool="shopware-entity-read", **over):
    """A discovery result record. The discovery fields are not optional —
    discovery_summary indexes them directly, so a record missing `steps` or
    `discovery_path` is not a shape the runner ever produces."""
    return {
        "id": fid,
        "passed": passed,
        "expected_tool": tool,
        "tokens": {"input": 10, "output": 2},
        "steps": 2,
        "discovery_path": "toolsets",
        "search_hit": None,
        "enabled_correct_toolset": passed,
        **over,
    }


def test_report_records_the_discovery_mode():
    """The `modes` wrapper outlives baseline's removal on purpose: eval/compare_runs
    and the committed result artifacts both index report["modes"]["discovery"]."""
    report = E.build_report("openai", "m", [{}], [result("a")], True, 6)

    assert set(report["modes"]) == {"discovery"}
    assert "discovery_summary" in report


def test_report_counts_passes_failures_and_skips_separately():
    discovery = [result("a"), result("b", passed=False), result("c", passed=False, skipped=True)]

    mode = E.build_report("openai", "m", [{}] * 3, discovery, True, 6)["modes"]["discovery"]

    assert (mode["passed"], mode["failed"], mode["skipped"]) == (1, 1, 1)


def test_report_attributes_by_tier():
    """by_tier drives the job summary's By-owner table, so a failure has to be
    attributed to the repository that owns the tool."""
    discovery = [result("c1"), result("d1", passed=False, tool="swag-dev-tools-load-skill")]

    report = E.build_report("openai", "m", [{}] * 2, discovery, True, 6)

    assert report["by_tier"]["dev-tools"]["failed_ids"] == ["d1"]
    assert report["by_tier"]["core"]["passed"] == 1


def test_report_of_a_run_with_no_results_is_an_empty_table_not_a_crash():
    report = E.build_report("openai", "m", [{}], None, True, 6)

    assert report["modes"] == {} and report["by_tier"] == {}


def test_report_is_json_serialisable():
    """It is written with json.dumps; a stray non-serialisable value would only
    surface at the very end of a paid run."""
    report = E.build_report("anthropic", "m", [{}], [result("a")], False, 8)

    assert json.loads(json.dumps(report))["system_prompt"] is False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------
def test_main_returns_one_on_a_configuration_error(monkeypatch, capsys):
    """Exit code, not a traceback — and without reaching the network."""
    monkeypatch.setattr("sys.argv", ["eval.runner", "--modes", "nonsense"])

    assert E.main() == 1
    assert "unknown mode" in capsys.readouterr().err
