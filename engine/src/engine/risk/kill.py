"""The kill switch: stop every bot, cancel every open order, and, if asked, sell every
open position (ARCHITECTURE §6, D43).

It comes as a command over Redis (control/listener.py), so it works with the API down.
In order:

1. Every running or paused bot is set `stopped` in `bots`, with why and when. From then
   on the RiskManager blocks their buys (it reads the status on every buy, D42), and a
   restarted engine doesn't load them.
2. The bot runner stops them: each finishes what it's doing, then never acts again.
3. Every open order in the mode is cancelled, every bot's, so a buy placed while step 2
   waited is cancelled too. Cancels are never blocked (D35).
4. With `close_positions`, every open position in the mode is sold whole, as a
   marketable limit at the bid, through the RiskManager (a sell is never blocked, D41).
   Its exits are numbered like a stop's (`<buy id minus b>x<n>`, bots/exits.py), with
   kind `kill_switch`, and the position is sold until it's gone: on each price, an exit
   the bid fell below is cancelled and placed again (`next_exit`). A position with no
   fresh price waits for one. One that can't be sold (below the market's minimums, or
   opened by no buy of its bot) is reported and left open.

A restarted engine carries on selling every position whose latest exit is the kill
switch's (`resume`), since the restart cancelled that exit (D38). It's worked out from
`orders`, so nothing extra is stored.

`close_bot()` sells one bot's position the same way, for "Stop + close position" on the
Bots page (D47, control/bots.py stops the bot first). Its exits have kind `close`, and
a restart carries on with them too. A strategy switch with the policy `close_first`
(D51) sells with it as well, with kind `switch`; the bot keeps running, and the bot
runner holds it back until its position is gone (`selling`).
"""

import asyncio
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.backtest.models import default_slippage
from engine.bots.exits import ExitCause, Wait, exit_order_id, load_held, next_exit
from engine.bots.orders import Skip, exit_order
from engine.bots.store import open_orders
from engine.brokers.paper import RulesSource
from engine.marketdata.quotes import MAX_QUOTE_AGE, QuoteSource
from engine.risk.manager import RiskManager
from shared.commands import Closing, KillCommand, KillReply, NotClosed
from shared.db.models import BotRow, PositionRow
from shared.prices import PriceUpdate

log = structlog.get_logger()

KILL_SWITCH: ExitCause = "kill_switch"  # the `kind` of its exits
CLOSE: ExitCause = "close"  # ... and of one bot's, closed from the Bots page
SWITCH: ExitCause = "switch"  # ... and of one bot's, closed before a strategy switch
_SELLING_KINDS = (KILL_SWITCH, CLOSE, SWITCH)


class Stoppable(Protocol):
    """The bot runner, as the kill switch sees it."""

    async def stop(self, bot_ids: Collection[int]) -> list[int]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Selling:
    """A position the kill switch is selling."""

    bot_id: int
    name: str
    exchange: str
    symbol: str
    command_id: UUID | None  # None if the command is unknown (an old exit without one)
    kind: ExitCause = KILL_SWITCH  # or CLOSE or SWITCH: one bot's, from the Bots page


class KillSwitch:
    def __init__(
        self,
        risk: RiskManager,
        db: AsyncEngine,
        rules: RulesSource,
        quotes: QuoteSource,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._risk = risk
        self._db = db
        self._rules = rules
        self._quotes = quotes
        self._clock = clock
        # Set once the bots are loaded; until then there's nothing running to stop.
        self.runner: Stoppable | None = None
        self._selling: dict[tuple[int, str], _Selling] = {}  # by (bot, symbol)
        # A kill and the prices that sell its positions take turns.
        self._lock = asyncio.Lock()

    @property
    def selling(self) -> list[tuple[int, str]]:
        """The (bot, symbol) positions it's still selling."""
        return list(self._selling)

    def sells_for(self, bot_id: int) -> bool:
        """Whether it's still selling a position of bot `bot_id`."""
        return any(selling_bot == bot_id for selling_bot, _ in self._selling)

    async def kill(self, command: KillCommand) -> KillReply:
        """Carry out `command` (module docstring) and say what it did.

        If stopping the bots fails, this raises and nothing else is done. After that,
        a failure to cancel or sell one order is logged, and the rest still happen.
        """
        started = time.monotonic()
        log.warning(
            "kill_switch.received",
            **command.model_dump(mode="json"),
            age_s=round(command.age(self._clock()), 3),
        )
        async with self._lock:
            stopped = await self._stop_bots(command)
            if self.runner is not None:
                await self.runner.stop(stopped)
            canceled = await self._cancel_all()
            closing: list[Closing] = []
            not_closed: list[NotClosed] = []
            if command.close_positions:
                for selling in await self._open_positions(command.id):
                    self._selling[(selling.bot_id, selling.symbol)] = selling
                    quote = await self._quotes.latest(selling.exchange, selling.symbol)
                    result = await self._sell_safely(selling, quote)
                    if isinstance(result, NotClosed):
                        not_closed.append(result)
                    elif result is not None:
                        closing.append(result)
        reply = KillReply(
            id=command.id,
            ok=True,
            stopped=stopped,
            canceled=canceled,
            closing=closing,
            not_closed=not_closed,
            took_ms=round((time.monotonic() - started) * 1000),
        )
        log.warning("kill_switch.done", **reply.model_dump(mode="json"))
        return reply

    async def close_bot(
        self, bot_id: int, command_id: UUID, *, kind: ExitCause = CLOSE
    ) -> tuple[list[Closing], list[NotClosed]]:
        """Sell bot `bot_id`'s open positions until they're gone, as the kill switch does,
        with exits of kind `kind`. Stop the bot first, or hold it back while `sells_for`
        it (a switch, D51), so it doesn't buy them back or sell them twice."""
        closing: list[Closing] = []
        not_closed: list[NotClosed] = []
        async with self._lock:
            for selling in await self._open_positions(command_id, bot_id=bot_id, kind=kind):
                self._selling[(selling.bot_id, selling.symbol)] = selling
                quote = await self._quotes.latest(selling.exchange, selling.symbol)
                result = await self._sell_safely(selling, quote)
                if isinstance(result, NotClosed):
                    not_closed.append(result)
                elif result is not None:
                    closing.append(result)
        return closing, not_closed

    async def on_price(self, update: PriceUpdate) -> None:
        """Sell on with `update` what's still being sold in its symbol.

        Call it after the broker has filled what `update` reaches. While a kill is
        under way it does nothing: the kill itself places the first exits.
        """
        if self._lock.locked():
            return
        due = [
            s
            for s in self._selling.values()
            if (s.exchange, s.symbol) == (update.exchange, update.symbol)
        ]
        if not due:
            return
        async with self._lock:
            for selling in due:
                await self._sell_safely(selling, update)

    async def resume(self) -> list[tuple[int, str]]:
        """At startup: carry on selling the positions whose latest exit is the kill
        switch's, or a close's. Returns them, as (bot, symbol)."""
        mode = self._risk.mode
        for selling in await self._open_positions(None):
            held = await load_held(self._db, selling.bot_id, selling.symbol, mode=mode)
            if held is None or not held.exits or held.exits[-1].kind not in _SELLING_KINDS:
                continue
            latest = await self._risk.get_order(held.exits[-1].client_order_id)
            exit_ = None if latest is None else latest.reason.get("exit")
            command = exit_.get("command") if isinstance(exit_, dict) else None
            key = (selling.bot_id, selling.symbol)
            self._selling[key] = _Selling(
                selling.bot_id,
                selling.name,
                selling.exchange,
                selling.symbol,
                UUID(command) if isinstance(command, str) else None,
                held.exits[-1].kind,
            )
            log.info(
                "kill_switch.resumed",
                bot_id=selling.bot_id,
                name=selling.name,
                symbol=selling.symbol,
                last_exit=held.exits[-1].client_order_id,
                amount=str(held.position.amount),
            )
        return self.selling

    # --- Internals ----------------------------------------------------------------------

    async def _stop_bots(self, command: KillCommand) -> list[int]:
        """Set every running or paused bot `stopped`, in every mode. Returns their ids."""
        reason = f"kill switch from {command.source} (command {command.id})"
        async with self._db.begin() as conn:
            ids = await conn.scalars(
                update(BotRow)
                .where(BotRow.status.in_(("running", "paused")))
                .values(
                    status="stopped",
                    stopped_reason=reason,
                    stopped_at=self._clock(),
                    paused_reason=None,
                    paused_at=None,
                )
                .returning(BotRow.id)
            )
            stopped = sorted(ids.all())
        log.info("kill_switch.stopped", bot_ids=stopped, reason=reason)
        return stopped

    async def _cancel_all(self) -> list[str]:
        """Cancel every open order in the mode, stopped bots' too."""
        canceled: list[str] = []
        for cid in await open_orders(self._db, mode=self._risk.mode):
            try:
                await self._risk.cancel(cid)
            except Exception as exc:
                log.error(
                    "kill_switch.cancel_failed",
                    client_order_id=cid,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                continue
            canceled.append(cid)
        log.info("kill_switch.canceled", client_order_ids=canceled)
        return canceled

    async def _open_positions(
        self, command_id: UUID | None, *, bot_id: int | None = None, kind: ExitCause = KILL_SWITCH
    ) -> list[_Selling]:
        query = (
            select(PositionRow.bot_id, BotRow.name, PositionRow.exchange, PositionRow.symbol)
            .join(BotRow, BotRow.id == PositionRow.bot_id)
            .where(PositionRow.mode == self._risk.mode, PositionRow.closed_at.is_(None))
            .order_by(PositionRow.id)
        )
        if bot_id is not None:
            query = query.where(PositionRow.bot_id == bot_id)
        async with self._db.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_Selling(r.bot_id, r.name, r.exchange, r.symbol, command_id, kind) for r in rows]

    async def _sell_safely(
        self, selling: _Selling, quote: PriceUpdate | None
    ) -> Closing | NotClosed | None:
        try:
            return await self._sell(selling, quote)
        except Exception as exc:
            # Kept on the list: the next price tries again.
            log.error(
                "kill_switch.error",
                bot_id=selling.bot_id,
                symbol=selling.symbol,
                error=type(exc).__name__,
                detail=str(exc),
            )
            why = f"{type(exc).__name__}: {exc} (tried again on the next price)"
            return NotClosed(bot_id=selling.bot_id, symbol=selling.symbol, reason=why)

    async def _sell(
        self, selling: _Selling, quote: PriceUpdate | None
    ) -> Closing | NotClosed | None:
        """Sell what's left of the position at `quote`, unless its exit still reaches the
        bid. None once it's gone; NotClosed (and off the list) if it can't be sold."""
        bot_id, symbol = selling.bot_id, selling.symbol
        mode = self._risk.mode
        fields: dict[str, JsonValue] = {"bot_id": bot_id, "name": selling.name, "symbol": symbol}
        position = await self._risk.position(bot_id, symbol)
        if position is None:
            self._closed(selling, fields)
            return None
        held = await load_held(self._db, bot_id, symbol, mode=mode)
        if held is None:
            return self._not_closed(selling, fields, "no buy of the bot opened it")
        now = self._clock()
        if quote is None or quote.bid is None or now - quote.ts > MAX_QUOTE_AGE:
            # Sold at the next fresh bid instead.
            log.info("kill_switch.waiting", **fields, reason="no fresh bid")
            return Closing(bot_id=bot_id, symbol=symbol, amount=position.amount)
        if held.exits and held.exits[-1].kind in _SELLING_KINDS:
            latest = held.exits[-1]
            if isinstance(next_exit(held.exits, quote.bid), Wait):
                order = await self._risk.get_order(latest.client_order_id)
                return Closing(
                    bot_id=bot_id,
                    symbol=symbol,
                    amount=position.amount,
                    client_order_id=latest.client_order_id,
                    status=latest.status,
                    filled=None if order is None else order.filled,
                )
        # Whatever is still open for the bot goes first (a stop's exit, our last one),
        # so nothing is reserved and nothing sells twice.
        canceled = []
        for cid in await open_orders(self._db, bot_id, mode=mode):
            await self._risk.cancel(cid)
            canceled.append(cid)
        position = await self._risk.position(bot_id, symbol)  # what's left to sell
        if position is None:
            self._closed(selling, fields)
            return None
        attempt = len(held.exits) + 1
        planned = exit_order(
            position,
            quote,
            await self._rules.market_rules(symbol),
            client_order_id=exit_order_id(held.buy_id, attempt),
            now=now,
            slippage=default_slippage(symbol),
        )
        if isinstance(planned, Skip):
            return self._not_closed(selling, fields, planned.reason)
        reason: dict[str, JsonValue] = {
            "exit": {
                "kind": selling.kind,
                "attempt": attempt,
                "bid": str(quote.bid),
                "price_ts": quote.ts.isoformat(),
                "command": None if selling.command_id is None else str(selling.command_id),
            },
            "bot": {"id": bot_id, "name": selling.name},
        }
        outcome = await self._risk.submit(bot_id, planned.request, reason)
        order = outcome.order
        log.info(
            "kill_switch.exit",
            **fields,
            client_order_id=planned.request.client_order_id,
            attempt=attempt,
            amount=str(planned.request.amount),
            limit=str(planned.request.price),
            decision=outcome.decision.decision,
            status=None if order is None else order.status,
            filled=None if order is None else str(order.filled),
            canceled=canceled,
            quote=quote.model_dump(mode="json"),
        )
        if order is None or order.status == "rejected":
            # Placing it again on every price would only add rejected orders.
            why = "blocked" if order is None else f"rejected: {order.reject_reason}"
            return self._not_closed(selling, fields, why)
        if await self._risk.position(bot_id, symbol) is None:
            self._closed(selling, fields)
        return Closing(
            bot_id=bot_id,
            symbol=symbol,
            amount=planned.request.amount,
            client_order_id=order.client_order_id,
            status=order.status,
            filled=order.filled,
        )

    def _closed(self, selling: _Selling, fields: dict[str, JsonValue]) -> None:
        if self._selling.pop((selling.bot_id, selling.symbol), None) is not None:
            log.info("kill_switch.closed", **fields)

    def _not_closed(
        self, selling: _Selling, fields: dict[str, JsonValue], reason: str
    ) -> NotClosed:
        self._selling.pop((selling.bot_id, selling.symbol), None)
        log.error("kill_switch.not_closed", **fields, reason=reason)
        return NotClosed(bot_id=selling.bot_id, symbol=selling.symbol, reason=reason)
