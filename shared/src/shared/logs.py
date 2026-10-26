"""structlog set up the same way for the engine, the API and every CLI: JSON lines."""

from typing import TextIO

import structlog


def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """JSON logs to `stream` (default stdout). CLIs whose stdout is data log to stderr."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        # Drops calls below LOG_LEVEL before they do any work.
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(stream),
    )
