"""The engine's heartbeat in Redis, so the dashboard can show whether it's alive (D45).

The engine SETs `ENGINE_HEARTBEAT_KEY` on every beat (every 10 s). Like prices, it never
expires: readers compare `ts` with the clock, so a dead engine shows up as an old beat,
not as a missing one. Docker's health check still watches the engine's own file.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ENGINE_HEARTBEAT_KEY = "engine:heartbeat"


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
