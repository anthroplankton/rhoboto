from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


def setup(
    level: int, *, use_rich: bool = True, file_config: dict | None = None
) -> logging.Logger:
    """
    Set up logging with Rich console output and optional plain text file output.

    Args:
        level: Logging level.
        use_rich: Whether to use Rich for console output (default True).
        file_config: Optional dict for file logging. Keys:
            - log_to_file (bool, default False): Whether to log to file.
            - log_dir (str, default "data/logs"): Directory for log files.
              Will be created if not exists.
            - log_filename (str, default "app.log"): Name of the log file.
            - max_bytes (int, default 10*1024*1024): Max size for rotating log files.
            - backup_count (int, default 5): Number of backup files to keep.
    """
    if file_config is None:
        file_config = {}
    handlers = []

    datefmt = "%Y-%m-%d %H:%M:%S"

    # Formatter for plain text output (file and fallback console)
    plain_formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s - %(name)s - %(message)s",
        datefmt=datefmt,
    )

    # Console Handler
    if use_rich:
        console_handler = RichHandler(
            console=Console(stderr=True),
            show_time=True,
            show_level=True,
            show_path=True,
            markup=True,
            rich_tracebacks=True,
        )
        # RichHandler will use its own formatting for time, level, etc.
        console_handler.setFormatter(
            logging.Formatter(fmt="%(message)s", datefmt=datefmt)
        )
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(plain_formatter, datefmt=datefmt)
    handlers.append(console_handler)

    # File Handler
    if file_config.get("log_to_file", False):
        log_dir = file_config.get("log_dir", "data/logs")
        log_filename = file_config.get("log_filename", "app.log")
        max_bytes = file_config.get("max_bytes", 10 * 1024 * 1024)
        backup_count = file_config.get("backup_count", 5)
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path / log_filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(plain_formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers)

    return logging.getLogger()
