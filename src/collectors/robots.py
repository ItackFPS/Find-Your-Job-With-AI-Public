"""robots.txt rules shared by every collector that reads public pages (plan F6.1).

A ``public_page`` collector (see ``BaseCollector.access_class``) must never request a
URL its site's robots.txt disallows. This module holds the parser (RFC 9309: groups,
product tokens, longest match, ``*`` and ``$``, percent-encoding normalisation) and a
per-collector cache that fetches each site's robots.txt once per run.

What an answer means follows RFC 9309 §2.3.1:

* HTTP 200: the rules it contains (an empty file allows everything);
* HTTP 4xx (404 included): no robots.txt, so no restriction;
* HTTP 5xx, any other status, or the site unreachable: **disallow everything**.

A collector can ask for the stricter reading (``strict=True``, e.g. France Travail web,
documented to crawl only with a robots.txt actually served): anything but HTTP 200 with a
non-empty body then disallows everything.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import urlsplit

import httpx

RobotsRule = tuple[bool, re.Pattern[str], int]  # (allow, pattern, length of the normalised path)

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def normalise(path: str) -> str:
    """RFC 9309 §2.2.2: compare paths percent-encoded. Escapes of unreserved characters
    are decoded, other escapes upper-cased, and non-ASCII characters UTF-8 encoded."""
    def unescape(m: re.Match[str]) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()

    decoded = _PERCENT.sub(unescape, path)
    return "".join(ch if ord(ch) < 128 else "".join(f"%{b:02X}" for b in ch.encode("utf-8"))
                   for ch in decoded)


def _product_token(user_agent: str) -> str:
    """``Name/2.0 (comment)`` -> ``name``: robots.txt groups name a product token."""
    return user_agent.strip().split("/", 1)[0].split()[0].lower() if user_agent.strip() else ""


def parse_robots(text: str, agent: str = "*") -> list[RobotsRule]:
    """Rules that apply to ``agent``, per RFC 9309; ``*`` and ``$`` supported.

    Consecutive ``User-agent:`` lines open one group; the first other line ends that run
    of agent lines, and the group's rules follow. Every group naming ``agent``'s product
    token (case-insensitive, ``Name/2.0`` matched as ``name``) applies, merged; when none
    does, the ``*`` groups apply.
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
            token = value.lower() if value.strip() == "*" else _product_token(value)
            if token:
                groups[-1][0].add(token)
            continue
        in_agent_lines = False
        if key in ("allow", "disallow") and groups and value:
            value = normalise(value)
            regex = "".join(".*" if ch == "*" else ("$" if ch == "$" and i == len(value) - 1 else re.escape(ch))
                            for i, ch in enumerate(value))
            groups[-1][1].append((key == "allow", re.compile(regex), len(value)))
    wanted = _product_token(agent) if agent.strip() != "*" else "*"
    chosen = [rules for agents, rules in groups if wanted in agents]
    if not chosen:
        chosen = [rules for agents, rules in groups if "*" in agents]
    return [rule for rules in chosen for rule in rules]


def robots_allows(rules: list[RobotsRule], path_and_query: str) -> bool:
    """Longest matching rule wins, measured on the normalised path as written in
    robots.txt (allow wins a tie); no matching rule means allowed."""
    path = normalise(path_and_query)
    best: tuple[int, bool] | None = None
    for allow, pattern, length in rules:
        if pattern.match(path):
            if best is None or length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return True if best is None else best[1]


def rules_from_response(status_code: int, text: str, agent: str, *, strict: bool = False) -> list[RobotsRule] | None:
    """The rules for ``agent`` from a robots.txt answer; ``None`` means disallow all.

    RFC 9309: 200 gives the file's rules, 4xx no restriction (``[]``), anything else
    disallows all. ``strict``: only a 200 with a non-empty body is usable."""
    if strict:
        return parse_robots(text, agent) if status_code == 200 and text.strip() else None
    if status_code == 200:
        return parse_robots(text, agent)
    if 400 <= status_code < 500:
        return []
    return None


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
    """robots.txt rules per site for one collector run; thread-safe; an unreachable or
    failing (5xx) robots.txt disallows the whole site."""

    def __init__(self, agent: str, user_agent: str, timeout: float = 15.0, *, strict: bool = False) -> None:
        self.agent = agent
        self.user_agent = user_agent
        self.timeout = timeout
        self.strict = strict
        self.unreachable: set[str] = set()  # sites whose robots.txt could not be fetched at all
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
                self.unreachable.add(site)
            else:
                rules = rules_from_response(status, text, self.agent, strict=self.strict)
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
