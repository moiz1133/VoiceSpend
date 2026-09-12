"""Structured logging setup."""

import logging
import sys

from app.core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()

    formatter = logging.Formatter(
        fmt=(
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","message":"%(message)s"}'
        ),
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]

    # Quiet down noisy third-party loggers by default.
    logging.getLogger("uvicorn.access").setLevel(settings.LOG_LEVEL.upper())
