"""The Market chart of one bot: its candles, its strategy's indicators and its trades (D46).

Read fresh from the database and Redis each time, like the Overview (D45); nothing here
is stored, and it works with the engine down.

- `candles`: the bot's series as stored: closed candles only, never made up (D25).
- `indicators`: the strategy's lines, each point over that candle's own warm-up window,
  so they're the values the strategy decided on (shared/chart.py). The warm-up comes
  from the candles before the first one shown.
- `forming`: the candle still open, as the engine last saw it on the exchange's kline
  stream (shared/candles.py), when it's newer than the last closed one. Display only.
- `trades`: the bot's orders that filled at all, each at its first fill's time and its
  average fill price, with what placed it: a signal, a stop, or the kill switch.
- `switches`: each switch of its strategy config, by hand or by the regime (P6-4), at
  the time it was made, with the strategy it switched to.

Money and prices are Decimal, sent as strings (D9); indicator values are floats, as
the strategies compute them.
"""

import asyncio
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue
from redis.asyncio import Redis
from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from shared.candles import TIMEFRAME_STEPS, FormingCandle, forming_key
from shared.chart import ChartLines, Pane, chart_lines, line_points
from shared.db.models import (
    BotRow,
    CandleRow,
    FillRow,
    OrderRow,
    StrategyConfigRow,
    StrategySwitchRow,
)

DEFAULT_CANDLES = 1000
MAX_CANDLES = 5000  # DESIGN §9: the chart pans and zooms smoothly with this many

log = structlog.get_logger()

# What placed an order: "signal", or the exit's kind (stop_loss, trailing_stop,
# take_profit, kill_switch, close), from its stored `reason`. History filters on it too.
ORDER_KIND = func.coalesce(OrderRow.reason["exit"]["kind"].astext, "signal")


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class ChartBot(_Out):
    id: int
    name: str
    exchange: str
    symbol: str
    timeframe: str
    status: str
    strategy: str | None  # None: no active config
    version: int | None
    params: dict[str, JsonValue] | None


class CandleView(_Out):
    ts: AwareDatetime  # open time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class PointView(_Out):
    ts: AwareDatetime  # the candle's open time
    values: dict[str, float]  # each line's value there


class IndicatorsView(_Out):
    pane: Pane
    names: list[str]
    guides: dict[str, float]  # flat levels, e.g. RSI's oversold and overbought
    points: list[PointView]  # none for a candle still warming up, or after a gap


class TradeMarker(_Out):
    order_id: int
    client_order_id: str
    side: str
    # "signal", or the exit's: stop_loss, trailing_stop, take_profit, kill_switch, close
    kind: str
    ts: AwareDatetime  # its first fill
    price: Decimal  # the average fill price
    amount: Decimal  # filled so far
    status: str


class SwitchMarker(_Out):
    """A switch of the bot's strategy config (P6-4, D54)."""

    id: int
    ts: AwareDatetime  # when it was made; the new config decides from the next close on
    trigger: str  # manual, rollback or regime
    regime: str | None  # the regime's switches: which regime called for it
    strategy: str  # the strategy it switched to
    reason: str


class Chart(_Out):
    server_time: AwareDatetime  # ages are worked out against this, not the browser's clock
    bot: ChartBot
    candles: list[CandleView]
    indicators: IndicatorsView | None  # None: no strategy, or one the chart can't draw
    forming: FormingCandle | None
    trades: list[TradeMarker]
    switches: list[SwitchMarker]


class ChartUpdate(_Out):
    """What changed since the browser's last candle; sent every second by the WebSocket."""

    server_time: AwareDatetime
    candles: list[CandleView]  # closed since `after`, oldest first
    points: list[PointView]  # the indicators at those candles
    forming: FormingCandle | None
    trades: list[TradeMarker] | None  # every trade again, or None when they haven't changed
    switches: list[SwitchMarker] | None  # likewise, the switches


async def read_bot(conn: AsyncConnection, bot_id: int) -> ChartBot | None:
    row = (
        await conn.execute(
            select(
                BotRow.id,
                BotRow.name,
                BotRow.exchange,
                BotRow.symbol,
                BotRow.timeframe,
                BotRow.status,
                StrategyConfigRow.strategy,
                StrategyConfigRow.version,
                StrategyConfigRow.params,
            )
            .select_from(BotRow)
            .outerjoin(StrategyConfigRow, StrategyConfigRow.id == BotRow.active_strategy_config_id)
            .where(BotRow.id == bot_id)
        )
    ).one_or_none()
    return None if row is None else ChartBot.model_validate(row._asdict())


def lines_of(bot: ChartBot) -> ChartLines | None:
    """The bot's indicator lines, or None if there's nothing to draw."""
    if bot.strategy is None or bot.params is None:
        return None
    try:
        return chart_lines(bot.strategy, bot.params)
    except ValueError as exc:
        log.warning("api.chart.no_lines", bot_id=bot.id, strategy=bot.strategy, detail=str(exc))
        return None


async def read_candles(
    conn: AsyncConnection,
    bot: ChartBot,
    lines: ChartLines | None,
    *,
    after: datetime | None,
    limit: int,
) -> tuple[list[CandleView], list[PointView]]:
    """The newest `limit` closed candles (after `after`, if given), oldest first, and the
    indicators at them, worked out with the candles before them as warm-up."""
    series = (
        CandleRow.exchange == bot.exchange,
        CandleRow.symbol == bot.symbol,
        CandleRow.timeframe == bot.timeframe,
    )
    newest = select(CandleRow).where(*series)
    if after is not None:
        newest = newest.where(CandleRow.ts > after)
    rows = list(
        reversed((await conn.execute(newest.order_by(CandleRow.ts.desc()).limit(limit))).all())
    )
    candles = [_candle(row) for row in rows]
    if lines is None or not rows:
        return candles, []
    # The warm-up before the first one: a point's window ends at its own candle.
    before = (
        await conn.execute(
            select(CandleRow.ts, CandleRow.close)
            .where(*series, CandleRow.ts < rows[0].ts)
            .order_by(CandleRow.ts.desc())
            .limit(lines.warmup - 1)
        )
    ).all()
    window = [*reversed(before), *rows]
    # Up to MAX_CANDLES x a warm-up window of float math: off the event loop.
    values = await asyncio.to_thread(
        line_points,
        lines,
        [row.ts for row in window],
        [float(row.close) for row in window],
        TIMEFRAME_STEPS[bot.timeframe],
        first=len(before),
    )
    points = [
        PointView(ts=row.ts, values=value)
        for row, value in zip(rows, values, strict=True)
        if value is not None
    ]
    return candles, points


def _candle(row: Row[Any]) -> CandleView:
    return CandleView(
        ts=row.ts, open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume
    )


async def read_trades(
    conn: AsyncConnection, bot_id: int, *, since: datetime | None
) -> list[TradeMarker]:
    """The bot's orders with at least one fill (from `since` on), by first fill."""
    fills = (
        select(
            FillRow.order_id,
            func.min(FillRow.ts).label("ts"),
            (func.sum(FillRow.price * FillRow.amount) / func.sum(FillRow.amount)).label("price"),
        )
        .group_by(FillRow.order_id)
        .subquery()
    )
    query = (
        select(
            OrderRow.id.label("order_id"),
            OrderRow.client_order_id,
            OrderRow.side,
            ORDER_KIND.label("kind"),
            fills.c.ts,
            fills.c.price,
            OrderRow.filled.label("amount"),
            OrderRow.status,
        )
        .join(fills, fills.c.order_id == OrderRow.id)
        .where(OrderRow.bot_id == bot_id, OrderRow.filled > 0)
        .order_by(fills.c.ts, OrderRow.id)
    )
    if since is not None:
        query = query.where(fills.c.ts >= since)
    return [TradeMarker.model_validate(row._asdict()) for row in (await conn.execute(query)).all()]


async def read_switches(
    conn: AsyncConnection, bot_id: int, *, since: datetime | None
) -> list[SwitchMarker]:
    """The bot's switches (from `since` on), oldest first."""
    query = (
        select(
            StrategySwitchRow.id,
            StrategySwitchRow.ts,
            StrategySwitchRow.trigger,
            StrategySwitchRow.regime,
            StrategyConfigRow.strategy,
            StrategySwitchRow.reason,
        )
        .join(StrategyConfigRow, StrategyConfigRow.id == StrategySwitchRow.to_config)
        .where(StrategySwitchRow.bot_id == bot_id)
        .order_by(StrategySwitchRow.id)
    )
    if since is not None:
        query = query.where(StrategySwitchRow.ts >= since)
    return [SwitchMarker.model_validate(row._asdict()) for row in (await conn.execute(query)).all()]


async def read_forming(
    redis: Redis, bot: ChartBot, last_closed: datetime | None
) -> FormingCandle | None:
    """The candle still open, if the engine streams this series and it's newer than
    `last_closed` (a candle that has closed is the stored one)."""
    raw = await redis.get(forming_key(bot.exchange, bot.symbol, bot.timeframe))
    if raw is None:
        return None
    forming = FormingCandle.model_validate_json(raw)
    if last_closed is not None and forming.ts <= last_closed:
        return None
    return forming


async def read_chart(
    conn: AsyncConnection, redis: Redis, bot: ChartBot, *, limit: int, now: datetime
) -> Chart:
    lines = lines_of(bot)
    candles, points = await read_candles(conn, bot, lines, after=None, limit=limit)
    since = candles[0].ts if candles else None
    indicators = None
    if lines is not None:
        indicators = IndicatorsView(
            pane=lines.pane, names=list(lines.names), guides=lines.guides, points=points
        )
    return Chart(
        server_time=now,
        bot=bot,
        candles=candles,
        indicators=indicators,
        forming=await read_forming(redis, bot, candles[-1].ts if candles else None),
        trades=await read_trades(conn, bot.id, since=since),
        switches=await read_switches(conn, bot.id, since=since),
    )


async def read_update(
    conn: AsyncConnection,
    redis: Redis,
    bot: ChartBot,
    lines: ChartLines | None,
    *,
    after: datetime | None,
    since: datetime | None,
    sent: Sequence[TradeMarker] | None,
    sent_switches: Sequence[SwitchMarker] | None = None,
    now: datetime,
) -> ChartUpdate:
    """What's new since the candle `after`, the trades `sent` and the switches
    `sent_switches` (None: none sent yet)."""
    if after is None:  # the browser had no candles: start it off
        candles, points = await read_candles(conn, bot, lines, after=None, limit=DEFAULT_CANDLES)
    else:
        candles, points = await read_candles(conn, bot, lines, after=after, limit=MAX_CANDLES)
    last_closed = candles[-1].ts if candles else after
    trades = await read_trades(conn, bot.id, since=since)
    switches = await read_switches(conn, bot.id, since=since)
    return ChartUpdate(
        server_time=now,
        candles=candles,
        points=points,
        forming=await read_forming(redis, bot, last_closed),
        trades=None if sent is not None and list(sent) == trades else trades,
        switches=(
            None if sent_switches is not None and list(sent_switches) == switches else switches
        ),
    )
