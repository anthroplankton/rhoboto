import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from tortoise import Tortoise

SQLITE_KEEPALIVE_INTERVAL_SECONDS = 0.01
SQLITE_OPERATION_TIMEOUT_SECONDS = 10.0

_sqlite_keepalive_task: asyncio.Task[None] | None = None


def get_model_modules(package: str = "models") -> dict:
    """
    Get model modules for Tortoise ORM.

    Args:
        package (str, optional): The package name to search for model files.
            Defaults to "models".

    Returns:
        dict: A dictionary mapping package names to their model modules.
    """
    models_path = Path(package)
    return {
        package: [f"{package}.{file.stem}" for file in models_path.glob("[!_]*.py")]
    }


def _is_sqlite_url(db_url: str) -> bool:
    return db_url.startswith("sqlite://")


async def _sqlite_keepalive_loop() -> None:
    # Intentionally tick the loop for aiosqlite thread-to-loop wakeups.
    while True:  # noqa: ASYNC110
        await asyncio.sleep(SQLITE_KEEPALIVE_INTERVAL_SECONDS)


async def _start_sqlite_keepalive(db_url: str) -> None:
    global _sqlite_keepalive_task  # noqa: PLW0603
    if not _is_sqlite_url(db_url):
        return
    if _sqlite_keepalive_task is not None and not _sqlite_keepalive_task.done():
        return
    _sqlite_keepalive_task = asyncio.create_task(
        _sqlite_keepalive_loop(),
        name="sqlite-aiosqlite-keepalive",
    )


async def _stop_sqlite_keepalive(db_url: str) -> None:
    global _sqlite_keepalive_task  # noqa: PLW0603
    if not _is_sqlite_url(db_url):
        return
    if _sqlite_keepalive_task is None:
        return
    _sqlite_keepalive_task.cancel()
    with suppress(asyncio.CancelledError):
        await _sqlite_keepalive_task
    _sqlite_keepalive_task = None


async def init_db(db_url: str) -> None:
    """
    Initializes Tortoise ORM with the given database URL and model modules.

    Args:
        db_url (str): Database connection URL for Tortoise ORM.

    Returns:
        None
    """
    logger = logging.getLogger("db")
    logger.info("Initializing Tortoise ORM with db_url: %s", db_url)
    await _start_sqlite_keepalive(db_url)
    try:
        await Tortoise.init(
            db_url=db_url,
            modules=get_model_modules(),
        )
        if _is_sqlite_url(db_url):
            await asyncio.wait_for(
                Tortoise.generate_schemas(),
                timeout=SQLITE_OPERATION_TIMEOUT_SECONDS,
            )
        else:
            await Tortoise.generate_schemas()
    except asyncio.CancelledError:
        logger.exception("Tortoise ORM initialization was cancelled.")
        with suppress(Exception):
            await Tortoise.close_connections()
        await _stop_sqlite_keepalive(db_url)
        raise
    except Exception:
        logger.exception("Failed to initialize Tortoise ORM.")
        with suppress(Exception):
            await Tortoise.close_connections()
        await _stop_sqlite_keepalive(db_url)
        raise
    logger.info("Tortoise ORM database initialized.")


async def close_db(db_url: str) -> None:
    """
    Closes Tortoise ORM connections.

    Args:
        db_url (str): Database connection URL for Tortoise ORM.

    Returns:
        None
    """
    await _start_sqlite_keepalive(db_url)
    try:
        if _is_sqlite_url(db_url):
            await asyncio.wait_for(
                Tortoise.close_connections(),
                timeout=SQLITE_OPERATION_TIMEOUT_SECONDS,
            )
        else:
            await Tortoise.close_connections()
    finally:
        await _stop_sqlite_keepalive(db_url)
