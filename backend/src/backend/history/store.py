"""History's reads (P5-6, D49): every order and every fill, filtered, a page at a time
or all of them as CSV.

Rows come straight from `orders` and `fills` (with the bot's name, and what placed
each order: a signal or an exit, as on the chart). An order's average fill price and
fees are summed from its fills. The CSV writes the database's exact values: NUMERIC's
own digits (never an exponent such as 1E-8) and UTC times to the microsecond, so an
export of a range equals the rows a query for it returns. Manila time is for display.
"""

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ColumnElement, Numeric, Select, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from backend.market.chart import ORDER_KIND
from shared.db.models import BotRow, FillRow, OrderRow

DEFAULT_ROWS = 100
MAX_ROWS = 500
CSV_BATCH = 500  # rows fetched from the database per chunk of the download

Kind = Literal[
    "signal", "stop_loss", "trailing_stop", "take_profit", "kill_switch", "close", "switch"
]
OrderStatus = Literal["open", "closed", "canceled", "rejected"]


class _Filters(BaseModel):
    """What both tables filter on. `start` is inclusive and `end` exclusive, so the
    days a range covers follow each other without overlapping."""

    # Query parameters: an unknown one is refused (422), not ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot_id: int | None = Field(default=None, ge=1)
    symbol: str | None = Field(default=None, min_length=3, max_length=32)
    side: Literal["buy", "sell"] | None = None
    kind: Kind | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class FillFilters(_Filters):
    pass


class OrderFilters(_Filters):
    status: OrderStatus | None = None


class _Page(BaseModel):
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)
    before: int | None = Field(default=None, ge=1)  # an id: the page goes back from it


class OrderPage(OrderFilters, _Page):
    pass


class FillPage(FillFilters, _Page):
    pass


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class OrderView(_Out):
    """One order. The CSV's columns are these fields, in this order."""

    id: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    bot_id: int
    bot_name: str
    mode: str
    exchange: str
    symbol: str
    client_order_id: str
    side: str
    kind: str  # signal, or the exit's: stop_loss, trailing_stop, take_profit, kill_switch, close
    status: str
    price: Decimal  # the limit
    amount: Decimal  # in the base currency
    filled: Decimal
    avg_fill_price: Decimal | None  # None until it fills
    # The fills' fees, when they're all in one currency (always, on paper).
    fees: Decimal | None
    fee_currency: str | None
    reject_reason: str | None


class FillView(_Out):
    """One fill, with its order's side and kind. The CSV's columns, in order."""

    id: int
    ts: AwareDatetime
    order_id: int
    client_order_id: str
    bot_id: int
    bot_name: str
    symbol: str
    side: str
    kind: str
    price: Decimal
    amount: Decimal  # in the base currency
    value: Decimal  # price x amount, in the quote currency
    fee: Decimal
    fee_currency: str


# Any select: its columns vary with the table.
type Rows = Select[*tuple[Any, ...]]

ORDER_COLUMNS = list(OrderView.model_fields)
FILL_COLUMNS = list(FillView.model_fields)


def _common(filters: _Filters, ts: Any) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.bot_id is not None:
        conditions.append(OrderRow.bot_id == filters.bot_id)
    if filters.symbol is not None:
        conditions.append(OrderRow.symbol == filters.symbol)
    if filters.side is not None:
        conditions.append(OrderRow.side == filters.side)
    if filters.kind is not None:
        conditions.append(ORDER_KIND == filters.kind)  # noqa: SIM300 (a SQL expression)
    if filters.start is not None:
        conditions.append(ts >= filters.start)
    if filters.end is not None:
        conditions.append(ts < filters.end)
    return conditions


def orders_query(filters: OrderFilters) -> Rows:
    """Every order the filters match, newest first."""
    one_currency = func.count(distinct(FillRow.fee_currency)) == 1
    totals = (
        select(
            FillRow.order_id,
            # trim_scale: NUMERIC division keeps ~20 places ("104.2500000000000000").
            func.trim_scale(
                func.sum(FillRow.price * FillRow.amount) / func.sum(FillRow.amount),
                type_=Numeric,
            ).label("avg_fill_price"),
            case((one_currency, func.sum(FillRow.fee))).label("fees"),
            case((one_currency, func.min(FillRow.fee_currency))).label("fee_currency"),
        )
        .group_by(FillRow.order_id)
        .subquery()
    )
    conditions = _common(filters, OrderRow.created_at)
    if filters.status is not None:
        conditions.append(OrderRow.status == filters.status)
    return (
        select(
            OrderRow.id,
            OrderRow.created_at,
            OrderRow.updated_at,
            OrderRow.bot_id,
            BotRow.name.label("bot_name"),
            OrderRow.mode,
            OrderRow.exchange,
            OrderRow.symbol,
            OrderRow.client_order_id,
            OrderRow.side,
            ORDER_KIND.label("kind"),
            OrderRow.status,
            OrderRow.price,
            OrderRow.amount,
            OrderRow.filled,
            totals.c.avg_fill_price,
            totals.c.fees,
            totals.c.fee_currency,
            OrderRow.reject_reason,
        )
        .join(BotRow, BotRow.id == OrderRow.bot_id)
        .outerjoin(totals, totals.c.order_id == OrderRow.id)
        .where(*conditions)
        .order_by(OrderRow.id.desc())
    )


def fills_query(filters: FillFilters) -> Rows:
    """Every fill the filters match (the range is on the fill's own time), newest first."""
    return (
        select(
            FillRow.id,
            FillRow.ts,
            FillRow.order_id,
            OrderRow.client_order_id,
            OrderRow.bot_id,
            BotRow.name.label("bot_name"),
            OrderRow.symbol,
            OrderRow.side,
            ORDER_KIND.label("kind"),
            FillRow.price,
            FillRow.amount,
            (FillRow.price * FillRow.amount).label("value"),
            FillRow.fee,
            FillRow.fee_currency,
        )
        .join(OrderRow, OrderRow.id == FillRow.order_id)
        .join(BotRow, BotRow.id == OrderRow.bot_id)
        .where(*_common(filters, FillRow.ts))
        .order_by(FillRow.id.desc())
    )


async def read_orders(conn: AsyncConnection, page: OrderPage) -> list[OrderView]:
    query = orders_query(page).limit(page.limit)
    if page.before is not None:
        query = query.where(OrderRow.id < page.before)
    return [OrderView.model_validate(row._asdict()) for row in (await conn.execute(query)).all()]


async def read_fills(conn: AsyncConnection, page: FillPage) -> list[FillView]:
    query = fills_query(page).limit(page.limit)
    if page.before is not None:
        query = query.where(FillRow.id < page.before)
    return [FillView.model_validate(row._asdict()) for row in (await conn.execute(query)).all()]


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")  # the stored digits; str() could give "1E-8"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    return str(value)


async def csv_rows(db: AsyncEngine, query: Rows, columns: list[str]) -> AsyncIterator[str]:
    """The query's rows as CSV text, header first, streamed from a server-side cursor so
    a long history never sits in memory at once."""
    out = io.StringIO()
    writer = csv.writer(out)

    def take() -> str:
        text = out.getvalue()
        out.seek(0)
        out.truncate()
        return text

    writer.writerow(columns)
    yield take()
    async with db.connect() as conn:
        result = await conn.stream(query)
        async for rows in result.partitions(CSV_BATCH):
            for row in rows:
                values = row._mapping
                writer.writerow([_cell(values[column]) for column in columns])
            yield take()
