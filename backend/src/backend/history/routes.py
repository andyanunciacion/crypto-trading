"""History (P5-6, D49): every order and every fill, with filters and CSV export.

All of it is on the `protected` router (app.py): a session is checked before anything
here runs. Read-only: the API never writes an order or a fill.

- `GET /history/orders` / `GET /history/fills`: a page, newest first. Filters: `bot_id`,
  `symbol`, `side`, `kind` (what placed the order), `status` (orders only), and a
  `start` (inclusive) to `end` (exclusive) range of aware times, on the order's
  `created_at` or the fill's `ts`. `limit` (100, at most 500) and `before` (an id) page.
- `GET /history/orders.csv` / `GET /history/fills.csv`: the same filters, every row,
  as CSV with the database's exact values (store.py). The browser names the file.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from backend.auth.deps import CurrentUser
from backend.history.store import (
    FILL_COLUMNS,
    ORDER_COLUMNS,
    FillFilters,
    FillPage,
    FillView,
    OrderFilters,
    OrderPage,
    OrderView,
    csv_rows,
    fills_query,
    orders_query,
    read_fills,
    read_orders,
)

log = structlog.get_logger()

router = APIRouter(tags=["history"])

CSV_TYPE = "text/csv; charset=utf-8"
# No file name: the dashboard's link names it with the Manila dates it shows.
ATTACHMENT = {"Content-Disposition": "attachment"}


@router.get("/history/orders")
async def orders(request: Request, page: Annotated[OrderPage, Query()]) -> list[OrderView]:
    async with request.app.state.db.connect() as conn:
        return await read_orders(conn, page)


@router.get("/history/orders.csv")
async def orders_csv(
    request: Request, filters: Annotated[OrderFilters, Query()], user: CurrentUser
) -> StreamingResponse:
    log.info("api.history.export", table="orders", user=user.username, **_shown(filters))
    rows = csv_rows(request.app.state.db, orders_query(filters), ORDER_COLUMNS)
    return StreamingResponse(rows, media_type=CSV_TYPE, headers=ATTACHMENT)


@router.get("/history/fills")
async def fills(request: Request, page: Annotated[FillPage, Query()]) -> list[FillView]:
    async with request.app.state.db.connect() as conn:
        return await read_fills(conn, page)


@router.get("/history/fills.csv")
async def fills_csv(
    request: Request, filters: Annotated[FillFilters, Query()], user: CurrentUser
) -> StreamingResponse:
    log.info("api.history.export", table="fills", user=user.username, **_shown(filters))
    rows = csv_rows(request.app.state.db, fills_query(filters), FILL_COLUMNS)
    return StreamingResponse(rows, media_type=CSV_TYPE, headers=ATTACHMENT)


def _shown(filters: OrderFilters | FillFilters) -> dict[str, object]:
    return filters.model_dump(mode="json", exclude_none=True)
