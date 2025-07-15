import logging
from pathlib import Path

from bot import Rhoboto, config
from utils import logger

logger = logger.setup(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    use_rich=config.USE_RICH_LOGGING,
    file_config={
        "log_to_file": config.LOG_TO_FILE,
        "log_dir": config.LOG_DIR,
        "log_filename": config.LOG_FILENAME,
    },
)


def get_cogs_modules(package: str = "cogs") -> list[str]:
    cogs_path = Path(package)
    return (
        [f"{package}.{file.stem}" for file in cogs_path.glob("[!_]*.py")]
        if cogs_path.exists()
        else []
    )


bot = Rhoboto(
    command_prefix=config.COMMAND_PREFIX,
    db_url=config.DATABASE_URL,
    initial_cogs=get_cogs_modules(),
)

try:
    bot.run(config.DISCORD_TOKEN, log_handler=None)
except KeyboardInterrupt:
    logger.info("KeyboardInterrupt received. Shutting down Rhoboto.")
finally:
    logger.info("Rhoboto shutdown complete.")
