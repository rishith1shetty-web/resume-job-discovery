"""Whether a host's ``robots.txt`` permits fetching one path.

Career-page validation is the first thing in this project that fetches a
*third party's* site rather than a portal we have an adapter for, so it is the
first thing that has to ask permission for itself. The portal adapters resolve
this at design time - each one documents which paths its host allows (see
``app.services.portal_seed``) - but a career page can be on any employer's
domain, which cannot be surveyed in advance. So it is asked at run time.

# Why not ``urllib.robotparser``

Two reasons, both of which change verdicts on real files.

**Group precedence.** Consecutive ``User-agent:`` lines form *one* group
sharing the rules that follow::

    User-agent: BadBot
    User-agent: *
    Disallow: /

The standard library builds the groups correctly here, but the failure mode of
a naive parser - starting a new group on every agent line, so ``*`` inherits
nothing - is exactly the mistake that silently turns "disallowed" into
"allowed". It is worth owning.

**Longest-match precedence.** The de-facto standard resolves conflicts by the
most specific rule, not the first one written, with ``Allow`` winning a tie::

    Disallow: /jobs/
    Allow: /jobs/search

``/jobs/search`` is *allowed*: the Allow rule is longer. ``robotparser``
returns the first matching rule instead, which refuses a page the host
deliberately opened. That pattern is common on careers hosts, so getting it
wrong loses real employers.

# What "no robots.txt" means

Allowed. A 404 is the host saying it publishes no restrictions, and treating
absence as prohibition would refuse most of the web. A 5xx or a timeout is
different: the host *has* an opinion and we could not read it, so we decline to
fetch. Erring toward not-fetching there costs one employer; erring the other
way means requesting something we were asked not to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest ``robots.txt`` this will read. Anything past this is not a rules
#: file, and a hostile or broken host must not be able to hand us a body large
#: enough to matter.
MAX_ROBOTS_BYTES = 512_000


@dataclass(frozen=True, slots=True)
class _Rule:
    """One ``Allow``/``Disallow`` line, kept with its own specificity."""

    path: str
    allowed: bool

    @property
    def precedence(self) -> tuple[int, int]:
        """Longest path wins; ``Allow`` breaks an exact-length tie.

        The tie-break direction is the one the standard specifies and the one
        a site author expects: writing ``Allow`` next to an equally specific
        ``Disallow`` is how a host carves an exception out of a broad rule.
        """
        return (len(self.path), 1 if self.allowed else 0)


def _matches(pattern: str, path: str) -> bool:
    """Glob match for a robots path pattern.

    Supports the two wildcards the standard defines - ``*`` for any run of
    characters and a trailing ``$`` anchoring the end - and treats everything
    else as a literal prefix, which is what the format actually is.
    """
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]

    if "*" not in pattern:
        if anchored:
            return path == pattern
        return path.startswith(pattern)

    segments = pattern.split("*")

    # The first segment is a prefix, always.
    if not path.startswith(segments[0]):
        return False
    cursor = len(segments[0])

    for segment in segments[1:-1]:
        if not segment:
            continue
        found = path.find(segment, cursor)
        if found < 0:
            return False
        cursor = found + len(segment)

    tail = segments[-1]
    if not tail:
        # Pattern ended in '*': anything left over matches, unless anchored,
        # in which case '*$' still matches any remainder.
        return True
    if anchored:
        return path.endswith(tail) and len(path) - len(tail) >= cursor
    return path.find(tail, cursor) >= 0


@dataclass(slots=True)
class RobotsPolicy:
    """The rules one host publishes, already narrowed to our user agent."""

    rules: tuple[_Rule, ...] = ()
    #: True when the file could not be read *and* the host clearly has one.
    #: Distinguished from "no rules" so the caller can decline rather than
    #: assume permission.
    unreadable: bool = False

    def allows(self, url: str) -> bool:
        """Is fetching this URL permitted?"""
        if self.unreadable:
            return False
        if not self.rules:
            return True

        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        # Rules are written in unescaped form more often than not, and a
        # percent-encoded path would otherwise dodge them.
        path = unquote(path)

        best: _Rule | None = None
        for rule in self.rules:
            if not _matches(rule.path, path):
                continue
            if best is None or rule.precedence > best.precedence:
                best = rule

        return True if best is None else best.allowed

    @classmethod
    def unreadable_policy(cls) -> RobotsPolicy:
        return cls(rules=(), unreadable=True)

    @classmethod
    def permissive(cls) -> RobotsPolicy:
        """No file, or an empty one: nothing is restricted."""
        return cls(rules=(), unreadable=False)

    @classmethod
    def parse(cls, body: str, *, user_agent: str) -> RobotsPolicy:
        """Read a ``robots.txt`` and keep the group that applies to us.

        The most specific matching group wins: a group naming our product token
        beats the wildcard group, which is the whole point of naming it. Only
        one group applies - rules are never merged across groups.
        """
        token = _product_token(user_agent)

        groups: list[tuple[set[str], list[_Rule]]] = []
        agents: set[str] = set()
        rules: list[_Rule] = []
        # True while reading agent lines that have not yet been followed by a
        # rule. This is what makes consecutive `User-agent:` lines one group.
        collecting_agents = False

        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue

            field_name, _, value = line.partition(":")
            field_name = field_name.strip().casefold()
            value = value.strip()

            if field_name == "user-agent":
                if not collecting_agents and agents:
                    # A rule intervened, so this starts a new group.
                    groups.append((agents, rules))
                    agents, rules = set(), []
                agents.add(value.casefold())
                collecting_agents = True
                continue

            if field_name in {"allow", "disallow"}:
                if not agents:
                    # A rule with no group above it applies to nobody.
                    continue
                collecting_agents = False
                if field_name == "disallow" and not value:
                    # "Disallow:" with an empty value means "allow everything",
                    # and must not be stored as a zero-length prefix rule -
                    # which would match every path.
                    continue
                if not value.startswith(("/", "*")):
                    # Not a path pattern. A rule may legitimately *begin* with
                    # a wildcard - UKG and UltiPro publish
                    # ``Allow: */JobBoard/`` under a blanket ``Disallow: /``,
                    # and rejecting those left every employer on those
                    # products looking forbidden when they are explicitly
                    # permitted.
                    continue
                rules.append(_Rule(path=value, allowed=field_name == "allow"))

        if agents:
            groups.append((agents, rules))

        for names, group_rules in groups:
            if token in names:
                return cls(rules=tuple(group_rules))

        for names, group_rules in groups:
            if "*" in names:
                return cls(rules=tuple(group_rules))

        return cls.permissive()


def _product_token(user_agent: str) -> str:
    """The comparable name from a User-Agent string.

    ``robots.txt`` names products, not full agent strings: our
    ``"JobFinderBot/0.1 (+...)"`` is addressed as ``jobfinderbot``.
    """
    head = user_agent.split("(", 1)[0].strip()
    return head.split("/", 1)[0].strip().casefold()


@dataclass(slots=True)
class RobotsCache:
    """One run's memory of what each host allows.

    Per run rather than process-wide: a long-lived cache would keep honouring a
    permission the host has since withdrawn, and a search is short enough that
    one fetch per host is already the whole saving.
    """

    _policies: dict[str, RobotsPolicy] = field(default_factory=dict)

    def get(self, origin: str) -> RobotsPolicy | None:
        return self._policies.get(origin)

    def put(self, origin: str, policy: RobotsPolicy) -> None:
        self._policies[origin] = policy


def robots_url(url: str) -> str:
    """The ``robots.txt`` that governs this URL's origin."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


__all__ = [
    "MAX_ROBOTS_BYTES",
    "RobotsCache",
    "RobotsPolicy",
    "origin_of",
    "robots_url",
]
