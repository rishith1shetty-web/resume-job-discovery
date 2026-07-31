"""Adapter registry.

The single indirection that keeps portal names out of the core search
service. Adapters self-register; the search service asks the registry for
instances by slug and iterates the result.

    for portal in registry.create_many(slugs):
        ...   # no branching on which portal this is

Phase 1 registers nothing - ``app/portals/adapters/`` is intentionally empty.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, TypeVar

from app.core.logging import get_logger
from app.portals.base import BasePortal
from app.portals.exceptions import PortalNotRegisteredError

logger = get_logger(__name__)

PortalT = TypeVar("PortalT", bound=type[BasePortal])


class PortalRegistry:
    """Maps ``slug -> adapter class``.

    Not a global singleton by accident: a fresh registry can be built in tests
    with fake adapters, which is how portal contract tests stay hermetic.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, type[BasePortal]] = {}

    def register(self, adapter: PortalT) -> PortalT:
        """Register an adapter class. Usable as a decorator."""
        slug = getattr(adapter, "slug", None)
        if not slug:
            raise ValueError(f"{adapter.__name__} must define a non-empty `slug`.")
        if slug in self._adapters and self._adapters[slug] is not adapter:
            raise ValueError(
                f"Portal slug {slug!r} is already registered to "
                f"{self._adapters[slug].__name__}."
            )
        self._adapters[slug] = adapter
        logger.debug("portal_adapter_registered", slug=slug)
        return adapter

    def unregister(self, slug: str) -> None:
        self._adapters.pop(slug, None)

    def is_registered(self, slug: str) -> bool:
        return slug in self._adapters

    def get(self, slug: str) -> type[BasePortal]:
        try:
            return self._adapters[slug]
        except KeyError:
            raise PortalNotRegisteredError(
                f"No adapter registered for portal {slug!r}.", portal=slug
            ) from None

    def create(self, slug: str, options: dict[str, Any] | None = None) -> BasePortal:
        """Instantiate one adapter with its database-supplied options."""
        return self.get(slug)(options=options)

    def create_many(
        self,
        slugs: Iterable[str],
        options_by_slug: dict[str, dict[str, Any]] | None = None,
    ) -> Iterator[BasePortal]:
        """Instantiate adapters for the caller's selected portals, in order."""
        options_by_slug = options_by_slug or {}
        for slug in slugs:
            yield self.create(slug, options_by_slug.get(slug))

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, slug: object) -> bool:
        return isinstance(slug, str) and slug in self._adapters


#: Process-wide registry populated by importing ``app.portals.adapters``.
registry = PortalRegistry()
