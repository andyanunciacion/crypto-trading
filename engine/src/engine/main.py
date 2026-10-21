"""Engine entry point. Run with `python -m engine`.

For now this is only a heartbeat loop, so Docker can tell the worker is alive.
Market data, strategies, the RiskManager and brokers plug into it in later phases.
"""

import asyncio
import signal
from pathlib import Path

import structlog

HEARTBEAT_INTERVAL_SECONDS = 10
# The Docker health check reads this file's modification time (see compose.yaml).
HEARTBEAT_FILE = Path("/tmp/engine-heartbeat")

log = structlog.get_logger()


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )


async def run() -> None:
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

    log.info("engine.starting", heartbeat_interval_s=HEARTBEAT_INTERVAL_SECONDS)
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
    configure_logging()
    asyncio.run(run())
