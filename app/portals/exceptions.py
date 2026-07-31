"""Portal failure taxonomy.

These are recorded against ``search_run_portals`` and ``portal_errors`` so a
run can complete with ``completed_with_errors`` instead of failing outright
(CLAUDE.md section 18).
"""

from __future__ import annotations


class PortalError(Exception):
    """Base for any portal-side failure. Always isolated to one portal."""

    code = "portal_error"

    def __init__(self, message: str, *, portal: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.portal = portal


class PortalTimeoutError(PortalError):
    code = "portal_timeout"


class PortalRateLimitedError(PortalError):
    """Portal asked us to slow down. Back off; never route around it."""

    code = "portal_rate_limited"


class PortalAuthError(PortalError):
    code = "portal_auth_failed"


class PortalBlockedError(PortalError):
    """Access denied by the portal (bot protection, terms restriction).

    CLAUDE.md section 15: respect this. Mark the portal unavailable rather
    than attempting to evade the block.
    """

    code = "portal_blocked"


class PortalParseError(PortalError):
    """Response arrived but did not match the expected shape."""

    code = "portal_parse_failed"


class PortalNotRegisteredError(PortalError):
    """No adapter is registered for the requested slug."""

    code = "portal_not_registered"


class PortalNotImplementedError(PortalError):
    """Portal row exists but its adapter has not been built yet."""

    code = "portal_not_implemented"
