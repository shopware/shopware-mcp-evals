"""The output-cap parameter probe in openai_turn.

`max_tokens` was rejected outright by every GPT-5 and o-series model, which
locked the eval to the gpt-4 family. `max_completion_tokens` works on all of
those *and* on gpt-4o/gpt-4.1 — but not on third-party OpenAI-compatible
endpoints (the `github` provider serves Mistral, which only knows the old
name), so the parameter is probed per model rather than hardcoded.
"""

from collections.abc import Iterable, Iterator
from types import SimpleNamespace

import pytest

from eval import runner as E


class FakeCompletions:
    """Records which cap parameter each call used; rejects the ones not in `accepts`."""

    def __init__(self, accepts: Iterable[str]) -> None:
        self.accepts: set[str] = set(accepts)
        self.calls: list[str | None] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        used = next((p for p in ("max_tokens", "max_completion_tokens") if p in kwargs), None)
        self.calls.append(used)
        if used not in self.accepts:
            raise RuntimeError(f"Unsupported parameter: '{used}' is not supported with this model.")
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
        )


def client_for(accepts: Iterable[str]) -> tuple[SimpleNamespace, FakeCompletions]:
    fake = FakeCompletions(accepts)
    return SimpleNamespace(chat=SimpleNamespace(completions=fake)), fake


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    E._OUTPUT_CAP_PARAM.clear()
    yield
    E._OUTPUT_CAP_PARAM.clear()


def test_modern_model_uses_max_completion_tokens_first() -> None:
    client, fake = client_for({"max_completion_tokens"})

    E.openai_turn(client, "gpt-5.4-mini", None, [], [])

    assert fake.calls == ["max_completion_tokens"]
    assert E._OUTPUT_CAP_PARAM["gpt-5.4-mini"] == "max_completion_tokens"


def test_legacy_endpoint_falls_back_to_max_tokens() -> None:
    """Mistral via the github provider only accepts the old name."""
    client, fake = client_for({"max_tokens"})

    E.openai_turn(client, "mistral-ai/mistral-medium-2505", None, [], [])

    assert fake.calls == ["max_completion_tokens", "max_tokens"]
    assert E._OUTPUT_CAP_PARAM["mistral-ai/mistral-medium-2505"] == "max_tokens"


def test_the_probe_is_paid_once_per_model() -> None:
    """A second call reuses the discovered parameter instead of re-probing."""
    client, fake = client_for({"max_tokens"})

    E.openai_turn(client, "legacy", None, [], [])
    E.openai_turn(client, "legacy", None, [], [])

    assert fake.calls == ["max_completion_tokens", "max_tokens", "max_tokens"]


def test_unrelated_errors_are_not_swallowed_by_the_retry() -> None:
    """A 500 or an auth failure must surface, not be masked as a param problem."""

    class Boom:
        def create(self, **_kwargs: object) -> SimpleNamespace:
            raise RuntimeError("500 Internal Server Error")

    client = SimpleNamespace(chat=SimpleNamespace(completions=Boom()))

    with pytest.raises(RuntimeError, match="500"):
        E.openai_turn(client, "gpt-5.4-mini", None, [], [])
    assert "gpt-5.4-mini" not in E._OUTPUT_CAP_PARAM


def test_a_cached_model_does_not_retry_on_failure() -> None:
    """Once the parameter is known, a later error is a real error."""
    E._OUTPUT_CAP_PARAM["gpt-5.4-mini"] = "max_completion_tokens"
    client, fake = client_for(set())  # rejects everything

    with pytest.raises(RuntimeError):
        E.openai_turn(client, "gpt-5.4-mini", None, [], [])

    assert fake.calls == ["max_completion_tokens"]
