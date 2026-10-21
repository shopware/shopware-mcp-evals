"""Typed stand-ins for the calls the tests take away from the network.

These replace one-line lambdas. A lambda's parameters cannot be annotated, so
`monkeypatch.setattr(M, "mcp_call", lambda *a, **k: {...})` left both its
arguments and its return value untyped — and pyright propagated that Unknown
into every assertion downstream of the stub.

`const` covers the overwhelming majority: a stub that ignores what it was asked
and answers the same thing every time.
"""

from collections.abc import Callable
from typing import Never

import pytest

from mcp_result import ToolOutcome, from_text


def const[T](value: T) -> Callable[..., T]:
    """A stub that ignores its arguments and always returns `value`."""

    def stub(*_args: object, **_kwargs: object) -> T:
        return value

    return stub


def raiser(exc: BaseException) -> Callable[..., Never]:
    """A stub that always raises — for the transport failures the runners have to
    survive rather than propagate."""

    def stub(*_args: object, **_kwargs: object) -> Never:
        raise exc

    return stub


def never(reason: str) -> Callable[..., Never]:
    """A stub that fails the test if it is ever called.

    `const(pytest.fail(...))` would not do: const evaluates its argument at
    monkeypatch time, so the failure fires immediately instead of on the call
    that was supposed to be impossible.
    """

    def stub(*_args: object, **_kwargs: object) -> Never:
        pytest.fail(reason)

    return stub


def reads_text(text_of: Callable[..., str]) -> Callable[..., ToolOutcome]:
    """A `read_result` stand-in for a test that only says what TEXT came back.

    Replaces patching `mcp_result_text`, which is gone: every reader goes through
    mcp_result now, so the stub has to hand back an outcome, built from the text
    exactly as a pre-#20948 server would have sent it.
    """

    def stub(resp: object, *_args: object, **_kwargs: object) -> ToolOutcome:
        return from_text(text_of(resp))

    return stub
