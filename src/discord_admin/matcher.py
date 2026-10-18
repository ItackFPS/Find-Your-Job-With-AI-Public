"""Ambiguity-safe 0/1/many resource matching.

Used for both "find the Discord channel named job-<slug>" and "find the
webhook named job-<slug>-webhook on that channel". The rule is the same in
both cases: zero matches means create, exactly one means adopt/reuse, and
two or more means STOP and report — never guess which one is "the" resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class MatchOutcome(Enum):
    NONE = "none"
    ONE = "one"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class MatchResult:
    outcome: MatchOutcome
    match: Any | None = None  # the single matched item, only set when outcome is ONE
    candidates: tuple[Any, ...] = ()  # all matches, only populated when AMBIGUOUS


def resolve_unique(items: list[T], predicate: Callable[[T], bool]) -> MatchResult:
    """Filter ``items`` by ``predicate`` and classify the result count.

    Never picks a "best" candidate among multiple matches — that decision
    always belongs to a human, via the reconciliation_issues registry.
    """
    matches = [item for item in items if predicate(item)]
    if not matches:
        return MatchResult(outcome=MatchOutcome.NONE)
    if len(matches) == 1:
        return MatchResult(outcome=MatchOutcome.ONE, match=matches[0])
    return MatchResult(outcome=MatchOutcome.AMBIGUOUS, candidates=tuple(matches))


def match_channel_by_name(channels: list[dict], name: str) -> MatchResult:
    """Exact, case-sensitive match on Discord channel ``name`` — Discord
    channel names are already lowercase-normalized by the API, so exact
    match is the correct comparison (no fuzzy matching, ever).
    """
    return resolve_unique(channels, lambda c: c.get("name") == name)


def match_webhook_by_name_prefix(webhooks: list[dict], name_prefix: str) -> MatchResult:
    """Match webhooks whose name starts with ``name_prefix`` (e.g.
    "job-83-webhook") — a prefix rather than exact match so a human-renamed
    webhook (e.g. "job-83-webhook (old)") is still recognized as belonging
    to this slug rather than silently ignored and duplicated.
    """
    return resolve_unique(webhooks, lambda w: str(w.get("name") or "").startswith(name_prefix))
