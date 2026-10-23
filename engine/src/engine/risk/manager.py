"""The RiskManager: every order goes signal → RiskManager → Broker (ARCHITECTURE §6, D35).

It holds the bot's broker and is the only thing that calls it: the bot runner gets a
RiskManager, never a Broker, and test_broker_boundary.py keeps it that way.

`submit()` decides on an order, logs the decision with its inputs, and places the
order only if it's allowed. The decision is stored in the order's `reason` next to the
signal, so each order records why it was placed and what risk made of it.

For now this is a stub (D8): every order is allowed unchanged. P4 adds sizing, stops
and limits in `_decide()`, where an order can be blocked or resized; callers already
handle `order=None`, so the wiring doesn't change.
"""

from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, JsonValue

from engine.brokers.base import Broker, BrokerOrder, Mode
from engine.exchange.models import Balance, OrderRequest
from engine.strategies.base import Position

# The same values as `risk_events.decision` (P4).
Decision = Literal["allowed", "blocked", "resized"]

log = structlog.get_logger()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskDecision(_Frozen):
    """What the RiskManager made of one order."""

    decision: Decision
    rule: str  # the rule that decided
    detail: str


class RiskOutcome(_Frozen):
    decision: RiskDecision
    order: BrokerOrder | None  # None when blocked: nothing reached the broker


PASS_THROUGH = RiskDecision(
    decision="allowed", rule="pass_through", detail="stub: every order is allowed until P4"
)


class RiskManager:
    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    @property
    def mode(self) -> Mode:
        return self._broker.mode

    async def submit(
        self, bot_id: int, request: OrderRequest, reason: dict[str, JsonValue]
    ) -> RiskOutcome:
        """Decide on an order and place it if allowed. `reason` is the signal behind it.

        Broker errors (e.g. `DuplicateClientOrderId`) pass up unchanged.
        """
        if "risk" in reason:
            raise ValueError("reason already has a 'risk' entry; the RiskManager adds its own")
        decision = self._decide(bot_id, request)
        # Logged before the broker is called, so a crash there still leaves the decision.
        log.info(
            "risk.decision",
            mode=self.mode,
            bot_id=bot_id,
            **request.model_dump(mode="json"),
            reason=reason,
            **decision.model_dump(),
        )
        order = await self._broker.place_order(
            bot_id, request, {**reason, "risk": decision.model_dump(mode="json")}
        )
        return RiskOutcome(decision=decision, order=order)

    async def cancel(self, client_order_id: str) -> BrokerOrder:
        """Cancel what's left of an order. Never blocked: cancelling only lowers risk, and
        the kill switch (P4) relies on it always working."""
        log.info("risk.cancel", mode=self.mode, client_order_id=client_order_id)
        return await self._broker.cancel_order(client_order_id)

    # --- Reads, passed through ----------------------------------------------------------

    async def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return await self._broker.get_order(client_order_id)

    async def balances(self, bot_id: int) -> dict[str, Balance]:
        return await self._broker.balances(bot_id)

    async def position(self, bot_id: int, symbol: str) -> Position | None:
        return await self._broker.position(bot_id, symbol)

    # --- Internals ----------------------------------------------------------------------

    def _decide(self, bot_id: int, request: OrderRequest) -> RiskDecision:
        # The stub. P4's sizing, stops and limits decide here, from the bot's balances,
        # positions and risk settings.
        return PASS_THROUGH
