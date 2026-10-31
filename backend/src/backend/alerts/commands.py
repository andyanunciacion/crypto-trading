"""Commands from the owner's Telegram chat: /status, /pnl, /pause, /kill (P7-2, D60).

They run in the alerts service, on the same bot as the alerts. Like the API, it never
trades: /kill and /pause send the engine the same `KillCommand` and `BotCommand` the
dashboard sends, over the Redis command list (shared/commands.py, D43), with source
`telegram`, and answer with the engine's reply.

- **Long polling.** `get_updates` waits up to `POLL_SECONDS` for the next message and
  returns at once when one comes. A webhook would need a public HTTPS address, and the
  dashboard is never on the open internet. Each update is handled, then the offset moves
  past it, which tells Telegram it's done; a crash re-handles at most the last batch (at
  least once, as the alerts are). A kill or a pause run twice does no harm.
- **Only the owner's chat.** Anything from another chat gets no reply (a reply would
  show the bot is alive) and is logged as `alerts.command.ignored`, with who sent it.
- **Nothing old runs.** The first batch after a start or an outage is the backlog of
  what came while nobody was reading: a command in it more than `MAX_AGE` old is
  answered "not run, send it again", so a /kill sent hours ago doesn't fire now. Later
  batches arrive as they're sent, so they aren't judged by their date, and a container
  clock that drifted (Docker Desktop's can, after the PC sleeps) can't refuse them.
- **Buttons.** /kill answers with "Stop all bots", "Stop all + close positions" and
  "Cancel" (DESIGN UX1: fast, but a stray tap can't fire it), and /pause alone with a
  button per running bot. The buttons work once, for `BUTTONS_SECONDS`, and only on a
  message this run sent: after a restart, old buttons say they've expired.
- **No double kill message.** The engine's `kill_switch.done` line is also an alert.
  A kill sent from here is put in `ShownKills` first, and the alerts loop skips its
  alert; if the engine didn't reply in time, it's taken out, so the alert still comes
  when the engine carries the kill out.
"""

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram import BotCommandScopeChat, Update
from telegram.error import BadRequest, NetworkError, TelegramError

from backend.alerts import replies
from backend.alerts.telegram import Buttons, TelegramBot, TelegramSender
from backend.dashboard.overview import read_overview
from shared.backoff import Backoff
from shared.commands import BotCommand, KillCommand, send
from shared.db.models import BotRow
from shared.heartbeat import ENGINE_HEARTBEAT_KEY, EngineHeartbeat

log = structlog.get_logger()

SOURCE = "telegram"
POLL_SECONDS = 30  # how long one get_updates waits for a message
POLL_LIMIT = 100  # Telegram's most updates in one batch
ALLOWED_UPDATES = ("message", "callback_query")  # messages and button presses
MAX_AGE = timedelta(minutes=2)  # a command older than this, in the backlog, isn't run
BUTTONS_SECONDS = 120.0  # how long a prompt's buttons work
REPLY_SECONDS = 10.0  # how long to wait for the engine, as the dashboard does
DB_SECONDS = 5.0  # a database read gives up after this, as the alerts' do

PromptKind = Literal["kill", "pause"]

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Reading what came (pure) -----------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    name: str  # lowercase, without the slash or a "@bot" suffix
    args: tuple[str, ...]


def parse(text: str) -> Parsed | None:
    """A command (`/pause btc-ema`, `/kill@MyBot`), or None if `text` isn't one."""
    words = text.split()
    if not words or not words[0].startswith("/"):
        return None
    name = words[0][1:].partition("@")[0].lower()
    return Parsed(name, tuple(words[1:])) if name else None


@dataclass(frozen=True)
class Incoming:
    """What we use of one update: a message, or a press on one of our buttons."""

    update_id: int
    chat_id: int | None
    user_id: int | None
    username: str | None
    date: datetime | None  # Telegram's time for the message (a press: its message's)
    text: str | None = None  # a message's
    button: str | None = None  # a press's data
    callback_id: str | None = None  # a press's, to answer it
    message_id: int | None = None  # the message whose button was pressed


def incoming(update: Update) -> Incoming:
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    user_id = user.id if user else None
    username = user.username if user else None
    if (pressed := update.callback_query) is not None:
        message = pressed.message
        return Incoming(
            update.update_id,
            chat_id,
            user_id,
            username,
            date=message.date if message else None,
            button=pressed.data or "",
            callback_id=pressed.id,
            message_id=message.message_id if message else None,
        )
    message = update.message
    return Incoming(
        update.update_id,
        chat_id,
        user_id,
        username,
        date=message.date if message else None,
        text=message.text if message else None,
    )


class ShownKills:
    """Kills sent from Telegram whose result the chat gets as the reply, so the alerts
    loop doesn't send it again as an alert (service.py). Only the latest are kept."""

    def __init__(self, size: int = 50) -> None:
        self._ids: deque[str] = deque(maxlen=size)

    def add(self, command_id: UUID) -> None:
        self._ids.append(str(command_id))

    def discard(self, command_id: UUID) -> None:
        with contextlib.suppress(ValueError):
            self._ids.remove(str(command_id))

    def __contains__(self, command_id: object) -> bool:
        return command_id in self._ids


# --- The bot ----------------------------------------------------------------------------


class CommandBot:
    def __init__(
        self,
        bot: TelegramBot,
        sender: TelegramSender,
        redis: Redis,
        db: AsyncEngine,
        *,
        chat_id: int,
        mode: Literal["paper", "live"],
        shown_kills: ShownKills,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: Backoff | None = None,
        reply_within: float = REPLY_SECONDS,
        poll_seconds: int = POLL_SECONDS,
    ) -> None:
        self._bot = bot
        self._sender = sender
        self._redis = redis
        self._db = db
        self._chat_id = chat_id
        self._mode = mode
        self._shown_kills = shown_kills
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._backoff = backoff or Backoff()
        self._reply_within = reply_within
        self._poll_seconds = poll_seconds
        self._offset: int | None = None  # the next update wanted; Telegram drops those before
        self._live = False  # False: the next batch is a backlog (see the module docstring)
        self._failing = False
        self._menu_set = False
        self._prompts: dict[int, tuple[PromptKind, float]] = {}  # message id -> kind, sent

    # --- The loop ---------------------------------------------------------------------

    async def run(self) -> None:
        """Answer commands as they come, until cancelled."""
        log.info("alerts.commands.listening", chat_id=self._chat_id)
        while True:
            try:
                await self.poll()
            except Exception as exc:  # cancelling raises CancelledError, which isn't caught
                delay = self._backoff.next_delay()
                if not self._failing:
                    log.warning(
                        "alerts.commands.poll_failed",
                        error=type(exc).__name__,
                        detail=self._sender.redact(str(exc)),
                        retry_in_s=round(delay, 2),
                    )
                self._failing = True
                self._live = False
                await self._sleep(delay)

    async def poll(self) -> int:
        """Handle the next batch of updates; returns how many there were."""
        await self._sender.ready()
        await self._set_menu()
        updates = await self._bot.get_updates(
            offset=self._offset,
            # A backlog is fetched at once; then each poll waits for the next message.
            timeout=self._poll_seconds if self._live else 0,
            allowed_updates=ALLOWED_UPDATES,
        )
        if self._failing:
            log.info("alerts.commands.poll_recovered")
        self._failing = False
        self._backoff.reset()
        live = self._live
        for update in updates:
            await self.handle(incoming(update), live=live)
            self._offset = update.update_id + 1
        # A full batch may have more of the backlog behind it.
        self._live = len(updates) < POLL_LIMIT
        return len(updates)

    async def _set_menu(self) -> None:
        """The commands in the chat's menu, for this chat only; tried once per start."""
        if self._menu_set:
            return
        try:
            await self._bot.set_my_commands(replies.MENU, scope=BotCommandScopeChat(self._chat_id))
        except BadRequest as exc:  # a NetworkError subclass, but trying again won't help
            self._menu_failed(exc)
        except NetworkError:
            raise  # polling would fail too: the loop backs off and tries again
        except TelegramError as exc:
            self._menu_failed(exc)
        self._menu_set = True

    def _menu_failed(self, exc: TelegramError) -> None:
        log.warning(
            "alerts.commands.menu_failed",
            error=type(exc).__name__,
            detail=self._sender.redact(str(exc)),
        )

    # --- One update -------------------------------------------------------------------

    async def handle(self, came: Incoming, *, live: bool) -> None:
        if came.chat_id != self._chat_id:
            log.warning(
                "alerts.command.ignored",
                chat_id=came.chat_id,
                user_id=came.user_id,
                username=came.username,
                text=(came.text or came.button or "")[:100],
            )
            return
        try:
            if came.button is not None:
                await self._pressed(came)
            elif came.text is not None:
                await self._message(came, live=live)
        except Exception as exc:
            log.warning(
                "alerts.command.failed",
                error=type(exc).__name__,
                detail=self._sender.redact(str(exc)),
            )
            with contextlib.suppress(Exception):
                await self._sender.send(
                    f"That didn't work ({type(exc).__name__}). The Logs page has the details."
                )

    async def _message(self, came: Incoming, *, live: bool) -> None:
        text = came.text or ""
        command = parse(text)
        if command is None:
            await self._sender.send(replies.HELP)
            return
        log.info("alerts.command.received", command=command.name, args=list(command.args))
        now = self._clock()
        if not live and came.date is not None and now - came.date > MAX_AGE:
            log.warning(
                "alerts.command.too_old",
                command=command.name,
                sent_at=came.date.isoformat(),
                age_s=round((now - came.date).total_seconds()),
            )
            await self._sender.send(replies.too_old(text, came.date, now))
            return
        match command.name:
            case "status":
                await self._status()
            case "pnl":
                await self._pnl()
            case "kill":
                await self._prompt("kill", replies.KILL_PROMPT, replies.KILL_BUTTONS)
            case "pause":
                await self._pause_command(command.args)
            case "help" | "start":  # Telegram sends /start when a chat with the bot opens
                await self._sender.send(replies.HELP)
            case _:
                await self._sender.send(replies.unknown(command.name))

    async def _prompt(self, kind: PromptKind, text: str, buttons: Buttons) -> None:
        message_id = await self._sender.prompt(text, buttons)
        now = self._monotonic()
        self._prompts = {
            mid: (k, at) for mid, (k, at) in self._prompts.items() if now - at <= BUTTONS_SECONDS
        }
        if message_id is not None:
            self._prompts[message_id] = (kind, now)

    async def _pressed(self, came: Incoming) -> None:
        data = came.button or ""
        log.info("alerts.command.pressed", button=data)
        if came.callback_id is not None:
            with contextlib.suppress(TelegramError):  # it only stops the button's spinner
                await self._bot.answer_callback_query(came.callback_id)
        if came.message_id is None:
            return  # a message too old for Telegram to say which
        # Popped, so a second press (a double tap) finds nothing and does nothing.
        prompt = self._prompts.pop(came.message_id, None)
        if prompt is None or self._monotonic() - prompt[1] > BUTTONS_SECONDS:
            log.info("alerts.command.expired", button=data)
            await self._sender.edit(came.message_id, replies.EXPIRED)
            return
        kind = prompt[0]
        if data == replies.CANCEL:
            await self._sender.edit(came.message_id, replies.CANCELLED)
        elif kind == "kill" and data in (replies.KILL_STOP, replies.KILL_CLOSE):
            await self._kill(came.message_id, close_positions=data == replies.KILL_CLOSE)
        elif kind == "pause" and data == replies.PAUSE_ALL:
            await self._pause_all(came.message_id)
        elif kind == "pause" and data.startswith(replies.PAUSE_PREFIX):
            bot_id = int(data.removeprefix(replies.PAUSE_PREFIX))
            name = (await self._names()).get(bot_id, f"bot {bot_id}")
            await self._sender.edit(came.message_id, replies.pause_chosen(name))
            line, ok = await self._pause_one(bot_id, name)
            await self._sender.send(replies.paused([line]) if ok else line)
        else:
            log.warning("alerts.command.unknown_button", button=data, prompt=kind)
            await self._sender.edit(came.message_id, replies.EXPIRED)

    # --- /status and /pnl -------------------------------------------------------------

    async def _status(self) -> None:
        now = self._clock()
        overview = await self._db_read(
            read_overview(self._db, self._redis, mode=self._mode, now=now), "overview"
        )
        if overview is None:
            raw = await self._redis.get(ENGINE_HEARTBEAT_KEY)
            beat = None if raw is None else EngineHeartbeat.model_validate_json(raw)
            await self._sender.send(replies.status_without_db(beat, now))
            return
        await self._sender.send(replies.status(overview))

    async def _pnl(self) -> None:
        overview = await self._db_read(
            read_overview(self._db, self._redis, mode=self._mode, now=self._clock()), "overview"
        )
        if overview is None:
            await self._sender.send(f"{replies.DB_DOWN} Try again in a minute.")
            return
        await self._sender.send(replies.pnl(overview))

    # --- /kill ------------------------------------------------------------------------

    async def _kill(self, message_id: int, *, close_positions: bool) -> None:
        command = KillCommand(close_positions=close_positions, source=SOURCE, sent_at=self._clock())
        fields = {"id": str(command.id), "close_positions": close_positions}
        log.warning("alerts.command.kill_sent", **fields)
        self._shown_kills.add(command.id)  # before the engine can log it (see the docstring)
        # Sent first, the prompt edited while the engine works: a kill shouldn't wait.
        waiting = asyncio.create_task(send(self._redis, command, reply_within=self._reply_within))
        try:
            await self._sender.edit(message_id, replies.kill_chosen(close_positions))
        finally:
            reply = await waiting
        if reply is None:
            self._shown_kills.discard(command.id)
            log.error("alerts.command.no_reply", waited_s=self._reply_within, **fields)
            await self._sender.send(replies.kill_no_reply(self._reply_within))
            return
        log.warning("alerts.command.kill_reply", reply=reply.model_dump(mode="json"), **fields)
        await self._sender.send(replies.kill_result(reply, await self._names()))

    # --- /pause -----------------------------------------------------------------------

    async def _pause_command(self, args: Sequence[str]) -> None:
        if not args:
            running = await self._running()
            if running is None:
                await self._sender.send(f"{replies.DB_DOWN} Use the bot's id: /pause <id>.")
            elif not running:
                await self._sender.send(replies.NO_BOT_RUNNING)
            else:
                await self._prompt("pause", replies.PAUSE_PROMPT, replies.pause_buttons(running))
            return
        found = await self._find(args[0])
        if found is None:
            await self._sender.send(replies.unknown_bot(args[0], await self._running()))
            return
        line, ok = await self._pause_one(*found)
        await self._sender.send(replies.paused([line]) if ok else line)

    async def _pause_all(self, message_id: int) -> None:
        await self._sender.edit(message_id, replies.pause_chosen("every running bot"))
        running = await self._running()
        if running is None:
            await self._sender.send(replies.DB_DOWN)
            return
        if not running:
            await self._sender.send(replies.NO_BOT_RUNNING)
            return
        results = [await self._pause_one(bot_id, name) for bot_id, name in running]
        lines = [line for line, _ in results]
        await self._sender.send(
            replies.paused(lines) if any(ok for _, ok in results) else "\n".join(lines)
        )

    async def _find(self, arg: str) -> tuple[int, str] | None:
        """The bot `arg` names (its name, or its id), or None. With the database down, an
        id still works: the engine checks it."""
        names = await self._db_read(self._read_names(), "bot_names")
        if arg.isdigit():
            bot_id = int(arg)
            return bot_id, (names or {}).get(bot_id, f"bot {bot_id}")
        if names is None:
            return None
        exact = [(i, n) for i, n in names.items() if n == arg]
        loose = [(i, n) for i, n in names.items() if n.casefold() == arg.casefold()]
        match = exact or loose
        return match[0] if len(match) == 1 else None

    async def _pause_one(self, bot_id: int, name: str) -> tuple[str, bool]:
        """Pause one bot; the reply's line, and whether it's paused."""
        now = self._clock()
        command = BotCommand(
            action="pause",
            bot_id=bot_id,
            source=SOURCE,
            sent_at=now,
            expires_at=now + timedelta(seconds=self._reply_within),
        )
        fields = {"id": str(command.id), "type": command.type, "action": "pause", "bot_id": bot_id}
        log.info("alerts.command.sent", **fields)
        reply = await send(self._redis, command, reply_within=self._reply_within)
        if reply is None:
            log.error("alerts.command.no_reply", waited_s=self._reply_within, **fields)
        elif not reply.ok:
            log.info("alerts.command.refused", error=reply.error, **fields)
        else:
            log.info("alerts.command.reply", reply=reply.model_dump(mode="json"), **fields)
        ok = reply is not None and reply.ok
        return replies.pause_result(name, reply, self._reply_within), ok

    # --- The database -----------------------------------------------------------------

    async def _names(self) -> dict[int, str]:
        return await self._db_read(self._read_names(), "bot_names") or {}

    async def _read_names(self) -> dict[int, str]:
        async with self._db.connect() as conn:
            return {row.id: row.name for row in await conn.execute(select(BotRow.id, BotRow.name))}

    async def _running(self) -> list[tuple[int, str]] | None:
        """The running bots (id, name), oldest first; None if the database didn't answer."""
        return await self._db_read(self._read_running(), "running_bots")

    async def _read_running(self) -> list[tuple[int, str]]:
        async with self._db.connect() as conn:
            rows = await conn.execute(
                select(BotRow.id, BotRow.name).where(BotRow.status == "running").order_by(BotRow.id)
            )
            return [(row.id, row.name) for row in rows]

    async def _db_read(self, read: Awaitable[T], what: str) -> T | None:
        """`read`, or None if it fails or takes more than `DB_SECONDS`."""
        try:
            async with asyncio.timeout(DB_SECONDS):
                return await read
        except Exception as exc:
            log.warning(
                "alerts.command.db_failed", read=what, error=type(exc).__name__, detail=str(exc)
            )
            return None
