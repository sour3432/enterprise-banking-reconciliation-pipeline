"""
Centralized logging configuration for the reconciliation pipeline.

Provides a single entry point to configure structured, production-style logging
across CLI runs, scheduled jobs, and local development.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


DEFAULT_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)


def configure_logging(
    level: str = "INFO",
    log_directory: Optional[Path] = None,
    *,
    log_to_file: bool = True,
    log_filename: str = "pipeline.log",
) -> None:
    """
    Configure root logging handlers once per process.

    Args:
        level: Logging level name (e.g. DEBUG, INFO, WARNING).
        log_directory: Optional directory for rotating file output.
        log_to_file: When True and ``log_directory`` is set, append to a log file.
        log_filename: File name used when file logging is enabled.
    """
    root = logging.getLogger()
    root.handlers.clear()

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_to_file and log_directory is not None:
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_directory / log_filename,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger under the application hierarchy."""
    return logging.getLogger(f"gb_recon.{name}")
