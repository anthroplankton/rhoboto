import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """Configuration loader for Rhoboto Discord bot."""

    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
    COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "$")
    BOT_ENV = os.getenv("BOT_ENV", "dev").lower()
    LOG_TO_FILE = os.getenv("LOG_TO_FILE", "False").lower() == "true"
    USE_RICH_LOGGING = os.getenv("USE_RICH_LOGGING", "True").lower() == "true"
    LOG_DIR = os.getenv("LOG_DIR", "data/logs")
    LOG_FILENAME = "rhoboto.log"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if BOT_ENV == "dev" else "INFO").upper()
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite://data/db.sqlite3")
    GOOGLE_SERVICE_ACCOUNT_PATH = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_PATH", "bot/service_account.json"
    )

    DEFAULT_EMBED_COLOR = 0x99CCFF
    PROCESSING_EMOJI = "<:haruka_math:1402204882492063825>"
    CONFUSED_EMOJI = "<:haruka_confused:1402850801608556574>"


config = Config()

if not config.DISCORD_TOKEN:
    error_message = "DISCORD_TOKEN is required"
    raise ValueError(error_message)
