"""Logging bootstrap for the app namespace."""

from __future__ import annotations

import logging
import os
import sys


def configure_logging() -> None:
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        app_logger.addHandler(handler)
    app_logger.propagate = False
