"""Failures we already know about, and what each one is waiting for.

A functional check that fails for a reason somebody has already filed should
say so where the reader sees it, in the annotation on the job. Before this
registry the second-order failure showed up as GitHub's bare "Process completed
with exit code 1" on a green job, and nothing told the reader whether it was
new, known or being fixed.

An entry matches a failure by its check label and by a fragment of its error,
so the same check failing for a different reason is reported as unexpected. A
matched failure is still recorded and still shown. It only stops counting
towards the exit code, because the build has nothing new to say about it.

When a known check passes again, the reporter says so, and the entry should be
deleted. An entry that nothing fails any more is how a registry like this goes
stale.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownFailure:
    """One failure that is understood, filed and waiting for a fix.

    `label` is the check's label as the Reporter records it, matched as a
    prefix, because a passing check may append detail (the order ids, say).
    `error` is a fragment the failure's error must contain. `waiting_for` says
    who has to move, in words a reader can act on.
    """

    label: str
    error: str
    issue: str
    url: str
    waiting_for: str
    why: str

    def matches_label(self, label: str) -> bool:
        return label.startswith(self.label)

    def matches(self, label: str, error: str) -> bool:
        return self.matches_label(label) and self.error in error

    def describe(self) -> str:
        return f"Known: {self.issue} ({self.url}), waiting for {self.waiting_for}. {self.why}"


KNOWN_FAILURES: tuple[KnownFailure, ...] = (
    KnownFailure(
        # functional/journeys.py SECOND_ORDER_CHECK. Spelled out rather than
        # imported, because journeys imports the reporter that imports this;
        # tests/test_known_failures.py holds the two together.
        label="a signed-in buyer can place a second order",
        error="Completed checkout sessions cannot be updated",
        issue="shopware/agentic-commerce#163",
        url="https://github.com/shopware/agentic-commerce/issues/163",
        waiting_for="a fix in agentic-commerce",
        why=(
            "Shopware retires the checkout id at completion and the plugin never hands out "
            "its successor, so a returning buyer only holds a dead id. It fails on every "
            "lane and every protocol revision."
        ),
    ),
    KnownFailure(
        # functional/oauth.py HEALTH_CHECK_GAP / HEALTH_CHECK_ERROR.
        label="MCP-bound token refused on /api/_info/system-health-check",
        error="answered HTTP 200 with the health report",
        issue="shopware/shopware#21862",
        url="https://github.com/shopware/shopware/pull/21862",
        waiting_for="the follow-up on shopware/shopware#21862",
        why=(
            "A token bound to /api/_mcp is refused on the rest of the Admin API, but the system "
            "health check still answers it with the full report. Without a token it is a 401."
        ),
    ),
    KnownFailure(
        # functional/oauth.py SNIPPETS_GAP / SNIPPETS_ERROR.
        label="MCP-bound token gets only the anonymous admin snippets",
        error="answered the full Administration snippet set",
        issue="shopware/shopware#21862",
        url="https://github.com/shopware/shopware/pull/21862",
        waiting_for="the follow-up on shopware/shopware#21862",
        why=(
            "/api/_admin/snippets is public and answers the login page's snippets without a token. "
            "With a token bound to /api/_mcp it answers every Administration snippet, as it does "
            "for an Admin API token."
        ),
    ),
)


def match(label: str, error: str, known: tuple[KnownFailure, ...] = KNOWN_FAILURES) -> KnownFailure | None:
    """The entry that explains this failure, or None when it is new."""
    return next((entry for entry in known if entry.matches(label, error)), None)


def match_label(label: str, known: tuple[KnownFailure, ...] = KNOWN_FAILURES) -> KnownFailure | None:
    """The entry for this check regardless of its error: a pass that may mean a fix."""
    return next((entry for entry in known if entry.matches_label(label)), None)
