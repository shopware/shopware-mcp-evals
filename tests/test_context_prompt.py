"""The context prompt: who gets one, how big it is, and what it buys.

"Context prompt" here means the MCP prompt capability — ShopwareContextPrompt
and its siblings — fetched via prompts/list + prompts/get and concatenated with
the server's own instructions.

Measured on a live trunk lane, and the reason this file exists:

    admin  20,606 chars  shopware-context, merchant-context, swag-dev-tools-context,
                         swag-dev-tools-suggest-tooling
    store     460 chars  none

The two endpoints' pass rates had been read side by side for months as though
they were the same measurement.
"""

from eval import runner, summary


def test_only_anthropic_takes_the_prompt_out_of_band():
    """The bug this file was opened for: the guard tested `== "openai"`, so the
    `github` arm ran with no context prompt at all while its report recorded
    `system_prompt: true`. A whole provider was silently measuring something
    else, and any new provider inherited it."""
    source = (runner.__file__).replace(".pyc", ".py")
    with open(source) as handle:
        body = handle.read()

    assert 'if provider != "anthropic" and system_prompt:' in body
    assert 'if provider == "openai" and system_prompt:' not in body


def test_the_inventory_distinguishes_off_from_empty(monkeypatch):
    """ "We turned it off" and "the server served nothing" are different facts,
    and the store endpoint genuinely is the second one. A boolean conflates
    them."""
    monkeypatch.setattr(runner, "mcp_init", lambda endpoint=None: ("sid", ""))
    _prompt, disabled = runner.fetch_system_prompt(None, enabled=False)

    assert disabled["disabled"] is True
    assert disabled["total_chars"] == 0


def test_the_report_carries_the_inventory_not_just_the_flag():
    report = runner.build_report(
        "openai",
        "gpt-5.4-mini",
        [{"id": "f"}],
        None,
        True,
        6,
        None,
        {"names": ["shopware-context"], "chars": {"shopware-context": 9862}, "total_chars": 20606, "sha256": "abc"},
    )

    assert report["context_prompt"]["total_chars"] == 20606
    assert report["context_prompt"]["names"] == ["shopware-context"]
    # The digest is what lets two runs be told apart when the prompt content
    # changes but the flag does not.
    assert report["context_prompt"]["sha256"] == "abc"


def _report(model, enabled, chars, names, passed, failed, server="http://x"):
    return {
        "server": server,
        "model": model,
        "system_prompt": enabled,
        "context_prompt": {"names": names, "chars": {}, "total_chars": chars},
        "modes": {"discovery": {"passed": passed, "failed": failed}},
    }


def test_the_delta_states_what_the_prompt_bought():
    """The A/B: same model, same fixtures, prompt on and off."""
    out = summary.render_prompt_delta(
        [
            _report("gpt-5.4-mini", True, 20606, ["shopware-context"], 90, 10),
            _report("gpt-5.4-mini", False, 0, [], 70, 30),
        ]
    )

    assert "20,606 characters of context prompt moved `gpt-5.4-mini` by +20 points" in out
    assert "worth it" in out


def test_a_prompt_that_changes_nothing_says_so():
    """A 20k-character tool guide that buys nothing is a finding, not a gap in
    the report."""
    out = summary.render_prompt_delta(
        [
            _report("m", True, 20000, ["shopware-context"], 80, 20),
            _report("m", False, 0, [], 80, 20),
        ]
    )

    assert "+0 points" in out and "no measurable effect" in out


def test_a_prompt_that_hurts_is_not_dressed_up():
    out = summary.render_prompt_delta(
        [
            _report("m", True, 20000, ["shopware-context"], 60, 40),
            _report("m", False, 0, [], 80, 20),
        ]
    )

    assert "-20 points" in out and "actively hurting" in out


def test_an_endpoint_with_no_prompt_is_called_out_as_incomparable():
    """The headline finding. Without this note the two rates sit next to each
    other and read as a tool-quality difference."""
    out = summary.render_prompt_delta(
        [
            _report("m", True, 20606, ["shopware-context"], 84, 16, server="http://admin"),
            _report("m", True, 460, [], 13, 87, server="http://store"),
        ]
    )

    assert "no context prompt at all" in out
    assert "not comparable" in out


def test_nothing_recorded_renders_nothing():
    """Older reports predate the inventory; they must not produce an empty table."""
    assert summary.render_prompt_delta([{"model": "m"}]) == ""
    assert summary.render_prompt_delta([]) == ""


def test_a_run_with_no_graded_fixtures_does_not_divide_by_zero():
    out = summary.render_prompt_delta([_report("m", True, 100, ["p"], 0, 0)])

    assert "—" in out


def test_the_delta_never_pairs_across_endpoints():
    """Matching on the model alone paired store's prompt-on run against admin's
    prompt-off run and reported the gap between two different endpoints as what
    the prompt was worth. Exactly one delta line is correct here."""
    out = summary.render_prompt_delta(
        [
            _report("m", True, 20606, ["shopware-context"], 84, 16, server="http://admin"),
            _report("m", False, 0, [], 65, 35, server="http://admin"),
            _report("m", True, 460, [], 13, 87, server="http://store"),
        ]
    )

    assert out.count("characters of context prompt moved") == 1
    assert "+19 points" in out
