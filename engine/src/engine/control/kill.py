"""Trip the kill switch: stop every bot and cancel every open order (D43).

Run inside the stack (Redis has no host port); it needs only Redis, not the API:
    docker compose run --rm engine python -m engine.control.kill
Add `--close-positions` to also sell every open position. It prints the engine's reply
as JSON (exit 0), or exits 1 if the engine reports an error or doesn't answer in time.
With no answer the command stays queued, and the engine carries it out when it next
starts, before it loads any bot. Bots stay stopped until set running again by SQL
(until P5), then `docker compose restart engine`.
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from redis.asyncio import Redis

from shared.commands import BLOCK_SECONDS, COMMANDS_KEY, KillCommand, KillReply, reply_key
from shared.config import load_settings

DEFAULT_TIMEOUT_SECONDS = 10


async def send(redis: Redis, command: KillCommand, *, reply_within: float) -> KillReply | None:
    """Queue `command` for the engine and wait up to `reply_within` seconds for its reply
    (Redis does the waiting, in BLPOP, `BLOCK_SECONDS` at a time).

    None if there's none by then; the command stays queued.
    """
    await redis.lpush(COMMANDS_KEY, command.model_dump_json())
    deadline = time.monotonic() + reply_within
    while (left := deadline - time.monotonic()) > 0:
        popped = await redis.blpop([reply_key(command.id)], timeout=min(BLOCK_SECONDS, left))
        if popped is not None:
            return KillReply.model_validate_json(popped[1])
    return None


async def run(args: argparse.Namespace, redis_url: str) -> int:
    command = KillCommand(
        close_positions=args.close_positions, source="cli", sent_at=datetime.now(UTC)
    )
    redis = Redis.from_url(redis_url)
    try:
        reply = await send(redis, command, reply_within=args.timeout)
    finally:
        await redis.aclose()
    if reply is None:
        print(
            f"No reply from the engine in {args.timeout:g} s (command {command.id}). If it's "
            "down, the kill stays queued and runs when it starts, before any bot loads. "
            "Check: docker compose ps engine",
            file=sys.stderr,
        )
        return 1
    print(reply.model_dump_json(indent=2))
    if not reply.ok:
        print(f"kill switch failed: {reply.error}", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--close-positions", action="store_true", help="also sell every open position"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for the engine's reply (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    settings = load_settings()
    raise SystemExit(asyncio.run(run(args, settings.redis_url)))


if __name__ == "__main__":
    main()
