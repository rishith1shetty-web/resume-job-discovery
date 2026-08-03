"""Portal read schemas.

Read-only in Phase 3. The dashboard needs the portal checkboxes to create a
search run, so ``GET /portals`` exists; enabling, disabling and configuring
portals belongs to Phase 4, when adapters exist to be enabled.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, computed_field


class PortalSummary(BaseModel):
    """One portal as the dashboard renders it.

    ``is_selectable`` is what the checkbox should honour: a portal is only
    usable once it is both enabled and backed by an implemented adapter.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str
    website_url: str | None = None
    is_enabled: bool
    is_implemented: bool = Field(
        description="False until an adapter exists and passes contract tests."
    )
    supports_date_filter: bool
    supports_description_fetch: bool
    requires_auth: bool
    notes: str | None = None
    display_order: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_selectable(self) -> bool:
        return self.is_enabled and self.is_implemented
