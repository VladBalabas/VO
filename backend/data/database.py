import asyncio

import asyncpg

from backend.data.settings import Settings


class AsyncpgDatabase:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Пул підключень до бази даних ще не готовий")
        return self._pool

    async def connect(self, attempts: int = 30, delay_seconds: float = 1.0) -> None:
        for attempt in range(1, attempts + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    host=self._settings.postgres_host,
                    port=self._settings.postgres_port,
                    database=self._settings.postgres_database,
                    user=self._settings.postgres_user,
                    password=self._settings.postgres_password,
                    min_size=1,
                    max_size=self._settings.database_pool_size,
                    command_timeout=5,
                )
                async with self.pool.acquire() as connection:
                    await connection.execute("SELECT 1")
                return
            except (OSError, asyncpg.PostgresError):
                await self.disconnect()
                if attempt == attempts:
                    raise
                await asyncio.sleep(delay_seconds)

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def is_ready(self) -> bool:
        if self._pool is None:
            return False
        try:
            async with self._pool.acquire(timeout=2) as connection:
                return await connection.fetchval("SELECT 1") == 1
        except (TimeoutError, OSError, asyncpg.PostgresError):
            return False
