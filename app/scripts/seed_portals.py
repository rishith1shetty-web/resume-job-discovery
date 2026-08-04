"""Create or refresh the portal rows for the registered adapters.

    python -m app.scripts.seed_portals

Idempotent, and deliberately a script rather than a migration: migrations carry
schema, not environment data, and an operator who disables a portal should not
have that undone by the next deployment.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.portals.adapters  # noqa: F401  - registers the adapters
from app.core.config import get_settings
from app.db.session import create_engine
from app.services.portal_seed import ensure_portals_seeded


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        actions = await ensure_portals_seeded(session)
        await session.commit()

    for slug, action in sorted(actions.items()):
        print(f"{slug:12} {action}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
