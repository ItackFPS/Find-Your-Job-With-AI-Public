"""robots.txt rules shared by every collector that reads public pages (plan F6.1).

A ``public_page`` collector (see ``BaseCollector.access_class``) must never request a
URL its site's robots.txt disallows. This module holds the parser (RFC 9309: groups,
longest match, ``*`` and ``$``) and a per-collector cache that fetches each site's
robots.txt once per run and **fails closed**: without a robots.txt actually served
(HTTP 200 with a non-empty body), nothing on that site is allowed.

RFC 9309 lets a 4xx answer mean "no robots.txt, allow all"; collectors here read public
sites conservatively, so any answer other than a real robots.txt means no page is read.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import urlsplit

import httpx

RobotsRule = tuple[bool, re.Pattern[str], int]  # (allow, pattern, length of the path as written)


def parse_robots(text: str, agent: str = "*") -> list[RobotsRule]:
    """Rules that apply to ``agent``, per RFC 9309; ``*`` and ``$`` supported.

    Consecutive ``User-agent:`` lines open one group, which the following rules belong
    to. Every group naming ``agent`` (case-insensitive) applies, merged; when none does,
    the ``*`` groups apply.
    """
    groups: list[tuple[set[str], list[RobotsRule]]] = []
    in_agent_lines = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not in_agent_lines:
                groups.append((set(), []))
                in_agent_lines = True
            groups[-1][0].add(value.lower())
        elif key in ("allow", "disallow"):
            in_agent_lines = False
            if groups and value:
                regex = "".join(".*" if ch == "*" else ("$" if ch == "$" and i == len(value) - 1 else re.escape(ch))
                                for i, ch in enumerate(value))
                groups[-1][1].append((key == "allow", re.compile(regex), len(value)))
    wanted = agent.lower()
    chosen = [rules for agents, rules in groups if wanted in agents]
    if not chosen:
        chosen = [rules for agents, rules in groups if "*" in agents]
    return [rule for rules in chosen for rule in rules]


def robots_allows(rules: list[RobotsRule], path_and_query: str) -> bool:
    """Longest matching rule wins, measured on the path as written in robots.txt (allow
    wins a tie); no matching rule means allowed."""
    best: tuple[int, bool] | None = None
    for allow, pattern, length in rules:
        if pattern.match(path_and_query):
            if best is None or length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return True if best is None else best[1]


def rules_from_response(status_code: int, text: str, agent: str) -> list[RobotsRule] | None:
    """The rules for ``agent`` from a robots.txt answer, or ``None`` when the answer is
    not a real robots.txt (any status but 200, or an empty body): fail closed."""
    if status_code != 200 or not text.strip():
        return None
    return parse_robots(text, agent)


def path_and_query(url: str) -> str:
    """The part of ``url`` robots.txt rules are matched against (path, then ``?query``)."""
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fetch_robots_txt(url: str, *, user_agent: str, timeout: float) -> tuple[int, str]:
    """GET one robots.txt: ``(status, body)``. Raises ``httpx.HTTPError`` when unreachable.

    Redirects are followed (RFC 9309 allows up to five). Tests replace this function so
    they never reach the network (tests/conftest.py).
    """
    resp = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
    return resp.status_code, resp.text


_fetch_robots_txt_live = fetch_robots_txt  # the real fetcher, for tests that drive it through a fake transport


class RobotsCache:
    """robots.txt rules per site for one collector run; thread-safe; fails closed."""

    def __init__(self, agent: str, user_agent: str, timeout: float = 15.0) -> None:
        self.agent = agent
        self.user_agent = user_agent
        self.timeout = timeout
        self._rules: dict[str, list[RobotsRule] | None] = {}
        self._why: dict[str, str] = {}
        self._lock = threading.Lock()

    def seed(self, site: str, rules: list[RobotsRule] | None, why: str = "") -> None:
        """Record rules a collector fetched itself (e.g. with its own HTTP client)."""
        with self._lock:
            self._rules[site] = rules
            if why:
                self._why[site] = why

    def _rules_for(self, site: str) -> list[RobotsRule] | None:
        with self._lock:
            if site in self._rules:
                return self._rules[site]
            try:
                status, text = fetch_robots_txt(f"{site}/robots.txt", user_agent=self.user_agent,
                                                timeout=self.timeout)
            except httpx.HTTPError as exc:
                rules, why = None, f"robots.txt unreachable ({type(exc).__name__})"
            else:
                rules = rules_from_response(status, text, self.agent)
                why = "" if rules is not None else (
                    f"robots.txt unavailable (HTTP {status}{', empty' if status == 200 else ''})")
            self._rules[site] = rules
            if why:
                self._why[site] = why
            return rules

    def allows(self, url: str) -> bool:
        rules = self._rules_for(origin(url))
        return rules is not None and robots_allows(rules, path_and_query(url))

    def refusal(self, url: str) -> str:
        """Why ``url`` is not allowed, for an error message."""
        site = origin(url)
        if self._rules_for(site) is None:
            return f"{self._why.get(site, 'robots.txt unavailable')}; nothing on {site} is read"
        return f"robots.txt disallows {path_and_query(url)}"
