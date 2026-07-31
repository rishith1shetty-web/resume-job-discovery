"""Portal integration layer - deliberately isolated from the core system.

Import direction is one-way: this package may import from ``app.core``, but
``app.services`` must never import a concrete adapter. It depends on
``BasePortal`` and ``PortalRegistry`` only, so adding, removing or disabling a
portal is a data change rather than a code change (CLAUDE.md sections 4, 18).
"""

from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalHealth,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
)
from app.portals.registry import PortalRegistry, registry

__all__ = [
    "BasePortal",
    "PortalCapabilities",
    "PortalHealth",
    "PortalRegistry",
    "PortalSearchRequest",
    "PortalSearchResult",
    "RawJob",
    "registry",
]
