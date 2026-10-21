"""Engine entry point. Run with `python -m engine`.

For now this is only a heartbeat loop, so Docker can tell the worker is alive.
Market data, strategies, the RiskManager and brokers plug into it in later phases.
"""

import asyncio
import signal
from pathlib import Path

import structlog
from shared.config import Settings, load_settings

HEARTBEAT_INTERVAL_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/engine-heartbeat")

log = structlog.get_logger()


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        # Drops calls below LOG_LEVEL before they do any work.
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


async def run(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Python running as PID 1 in a container ignores SIGTERM unless it has a handler,
    # so without this `docker compose stop` would wait 10 s and then kill the engine.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops don't support signal handlers; Ctrl+C still works.
            pass

    log.info(
        "engine.starting",
        mode=settings.mode,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_SECONDS,
    )
    beat = 0
    while not stop.is_set():
        beat += 1
        HEARTBEAT_FILE.touch()
        log.info("engine.heartbeat", beat=beat)
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            pass
    log.info("engine.stopping")


def main() -> None:
    # First, so a bad config stops the engine before it does anything else.
    settings = load_settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))
