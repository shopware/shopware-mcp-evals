"""Cached-token accounting in the provider adapters.

The two providers report the same fact in opposite directions: OpenAI's
`prompt_tokens` INCLUDES the cached prefix, Anthropic's `input_tokens` excludes
it. Both adapters normalise to one shape so a single pricing table can serve
both — and getting the direction wrong is silent, producing a bill that is
wrong by exactly the cached amount with nothing to notice it.

This lives apart from tests/test_eval_adapters.py because that module stubs out
both turn functions for every test in the file; these have to call the real
ones.
"""

from types import SimpleNamespace

from eval import runner as E


def openai_response(prompt_tokens, completion_tokens=5, cached=None):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    if cached is not None:
        usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached)
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def anthropic_response(input_tokens, output_tokens=5, cache_read=None):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    if cache_read is not None:
        usage.cache_read_input_tokens = cache_read
    return SimpleNamespace(content=[], usage=usage, stop_reason="end_turn")


def client_returning(response, provider):
    if provider == "openai":
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: response)))
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: response))


def test_openai_cached_tokens_are_subtracted_from_the_full_price_bucket():
    """OpenAI's prompt_tokens INCLUDES the cached prefix. Not subtracting would
    bill the same tokens twice — once at full price and once at the discount."""
    E._OUTPUT_CAP_PARAM.clear()
    turn = E.openai_turn(client_returning(openai_response(1000, cached=800), "openai"), "m", None, [], [])

    assert turn["tokens"] == {"input": 200, "cached_input": 800, "output": 5}


def test_anthropic_cached_tokens_are_not_subtracted():
    """Anthropic's input_tokens is already the uncached remainder. Subtracting
    here — the mirror of the OpenAI bug — would under-count the bill."""
    turn = E.anthropic_turn(client_returning(anthropic_response(200, cache_read=800), "anthropic"), "m", None, [], [])

    assert turn["tokens"] == {"input": 200, "cached_input": 800, "output": 5}


def test_both_providers_normalise_to_the_same_shape():
    """Same real usage, opposite wire conventions, identical buckets out — this
    is what lets one pricing table serve both."""
    E._OUTPUT_CAP_PARAM.clear()
    openai = E.openai_turn(client_returning(openai_response(1000, cached=800), "openai"), "m", None, [], [])
    anthropic = E.anthropic_turn(
        client_returning(anthropic_response(200, cache_read=800), "anthropic"), "m", None, [], []
    )

    assert openai["tokens"] == anthropic["tokens"]


def test_a_provider_reporting_no_cache_detail_bills_everything_at_full_price():
    """Third-party OpenAI-compatible endpoints omit the field entirely."""
    E._OUTPUT_CAP_PARAM.clear()
    turn = E.openai_turn(client_returning(openai_response(300), "openai"), "m", None, [], [])
    assert turn["tokens"] == {"input": 300, "cached_input": 0, "output": 5}

    turn = E.anthropic_turn(client_returning(anthropic_response(300), "anthropic"), "m", None, [], [])
    assert turn["tokens"] == {"input": 300, "cached_input": 0, "output": 5}
