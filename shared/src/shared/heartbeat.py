"""The engine's heartbeat in Redis, so the dashboard can show whether it's alive (D45),
and the backtester's, for Settings → System (P7-4, D62).

The engine SETs `ENGINE_HEARTBEAT_KEY` on every beat (every 10 s), and the backtester
`BACKTESTER_HEARTBEAT_KEY`. Like prices, they never expire: readers compare `ts` with the
clock, so a dead service shows up as an old beat, not as a missing one. Docker's health
checks still watch each service's own file. The alerts service's status
(backend/alerts/status.py) is its heartbeat.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ENGINE_HEARTBEAT_KEY = "engine:heartbeat"
# A beat older than this means the engine is down or stuck (DESIGN UX5; Docker's health
# check uses the same 30 s). The dashboard has its own copy in frontend/lib/overview.ts.
ENGINE_STALE_SECONDS = 30


class EngineHeartbeat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: AwareDatetime  # when this beat was written
    started_at: AwareDatetime  # when the engine started
    mode: Literal["paper", "live"]  # the engine's global MODE
    beat: int = Field(ge=1)  # beats since it started
    exchange: str  # CCXT id of the exchange it streams prices from, e.g. "mexc"
    symbols: list[str]  # the symbols it streams prices for (shared/prices.py)
    # The bots it's running, warmed up (D47): a bot just started is "running" in the
    # database a few seconds before it's here. Empty in a beat from before P5-4.
    bots: list[int] = Field(default_factory=list)


BACKTESTER_HEARTBEAT_KEY = "backtester:heartbeat"
# Its beat comes every 10 s, also while a backtest runs (as Docker's check, 30 s).
BACKTESTER_STALE_SECONDS = 30


class ServiceHeartbeat(BaseModel):
    """A beat from a service with nothing more to say than that it's alive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: AwareDatetime  # when this beat was written
    started_at: AwareDatetime  # when the service started
