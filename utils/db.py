import logging
from pathlib import Path

from tortoise import Tortoise


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
    await Tortoise.init(
        db_url=db_url,
        modules=get_model_modules(),
    )
    await Tortoise.generate_schemas()
    logger.info("Tortoise ORM database initialized.")
