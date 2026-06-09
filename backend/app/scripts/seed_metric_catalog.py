import asyncio

from app.core.database import async_session_factory
from app.services.metrics.metric_catalog_seeder import seed_system_metrics


async def main() -> int:
    async with async_session_factory() as db:
        n = await seed_system_metrics(db)
        await db.commit()
        print(f"seeded {n} system metrics", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
