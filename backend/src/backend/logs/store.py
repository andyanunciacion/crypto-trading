"""The Logs page's reads (P5-6, D49): the services' mirrored log lines, and the risk
events table.

- Log lines come from the capped Redis stream the engine, the backtester and the API
  write (shared/logs.py), newest first. Filters are applied here, a batch of entries at
  a time, until a page is full or the stream runs out; a line that doesn't read is
  skipped. The stream holds the last ~50,000 lines: older ones are gone, and stdout
  (`docker compose logs`) is the full record.
- Risk events are the `risk_events` rows (D41, D42): every buy a limit or circuit
  breaker blocked or shrank, with the settings and account numbers it was checked
  against. A blocked buy has no order, so this is the only place it shows.
"""

from typing import Annotated, Literal, Self, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncConnection

from shared.db.models import BotRow, RiskEventRow
from shared.logs import LOGS_KEY, LogEntry, read_entry

DEFAULT_ROWS = 100
MAX_ROWS = 500
SCAN_BATCH = 1000  # stream entries read per round trip while filtering

# Each level and the ones above it.
LEVELS = {
    "info": frozenset({"info", "warning", "error", "critical"}),
    "warning": frozenset({"warning", "error", "critical"}),
    "error": frozenset({"error", "critical"}),
}


class LogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["info", "warning", "error"] | None = None  # this level and above
    # Prefixes, any of which an event may start with: `event=risk.&event=bot.order`.
    event: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=10
    )
    bot_id: int | None = Field(default=None, ge=1)
    service: Literal["engine", "backtester", "api", "alerts"] | None = None
    before: str | None = Field(default=None, pattern=r"^\d{1,20}-\d{1,20}$")  # a stream id
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)

    def matches(self, entry: LogEntry) -> bool:
        return (
            (self.level is None or entry.level in LEVELS[self.level])
            and (self.event is None or entry.event.startswith(tuple(self.event)))
            and (self.bot_id is None or entry.bot_id == self.bot_id)
            and (self.service is None or entry.service == self.service)
        )


class LogPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: list[LogEntry]  # newest first
    # Where the next page starts (pass it as `before`); None when the stream has no more.
    next: str | None


async def read_logs(redis: Redis, query: LogQuery) -> LogPage:
    found: list[LogEntry] = []
    # "(" makes the bound exclusive: the page starts after the last one shown.
    newest = "+" if query.before is None else f"({query.before}"
    while True:
        batch = cast(
            list[tuple[bytes, dict[bytes, bytes]]],
            await redis.xrevrange(LOGS_KEY, max=newest, min="-", count=SCAN_BATCH),
        )
        for raw_id, fields in batch:
            stream_id = raw_id.decode()
            entry = read_entry(stream_id, fields.get(b"json", b""))
            if entry is not None and query.matches(entry):
                found.append(entry)
                if len(found) == query.limit:
                    return LogPage(entries=found, next=stream_id)
        if len(batch) < SCAN_BATCH:
            return LogPage(entries=found, next=None)
        newest = f"({batch[-1][0].decode()}"


class RiskEventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot_id: int | None = Field(default=None, ge=1)
    rule: str | None = Field(default=None, min_length=1, max_length=64)
    decision: Literal["allowed", "blocked", "resized"] | None = None
    start: AwareDatetime | None = None  # inclusive
    end: AwareDatetime | None = None  # exclusive
    before: int | None = Field(default=None, ge=1)  # an id
    limit: int = Field(default=DEFAULT_ROWS, ge=1, le=MAX_ROWS)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class RiskEventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    ts: AwareDatetime
    mode: str
    bot_id: int
    bot_name: str
    client_order_id: str  # the order's, placed or not
    rule: str
    decision: str  # blocked or resized
    detail: str
    inputs: dict[str, JsonValue]  # the order, the settings and the account numbers


async def read_risk_events(conn: AsyncConnection, query: RiskEventQuery) -> list[RiskEventView]:
    conditions: list[ColumnElement[bool]] = []
    if query.bot_id is not None:
        conditions.append(RiskEventRow.bot_id == query.bot_id)
    if query.rule is not None:
        conditions.append(RiskEventRow.rule == query.rule)
    if query.decision is not None:
        conditions.append(RiskEventRow.decision == query.decision)
    if query.start is not None:
        conditions.append(RiskEventRow.ts >= query.start)
    if query.end is not None:
        conditions.append(RiskEventRow.ts < query.end)
    if query.before is not None:
        conditions.append(RiskEventRow.id < query.before)
    rows = (
        await conn.execute(
            select(
                RiskEventRow.id,
                RiskEventRow.ts,
                RiskEventRow.mode,
                RiskEventRow.bot_id,
                BotRow.name.label("bot_name"),
                RiskEventRow.client_order_id,
                RiskEventRow.rule,
                RiskEventRow.decision,
                RiskEventRow.detail,
                RiskEventRow.inputs,
            )
            .join(BotRow, BotRow.id == RiskEventRow.bot_id)
            .where(*conditions)
            .order_by(RiskEventRow.id.desc())
            .limit(query.limit)
        )
    ).all()
    return [RiskEventView.model_validate(row._asdict()) for row in rows]
