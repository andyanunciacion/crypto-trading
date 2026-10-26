"""The RiskManager: every order goes signal → RiskManager → Broker (ARCHITECTURE §6, D35).

It holds the bot's broker and is the only thing that calls it: the bot runner gets a
RiskManager, never a Broker, and test_broker_boundary.py keeps it that way.

`submit()` decides on an order, logs the decision with its inputs, and places the
order only if it's allowed, maybe smaller. The decision is stored in the order's
`reason` next to the signal, so each order records why it was placed and what risk
made of it.

The decision (D41):
- A sell, a strategy's or a stop's, is always allowed: it can only lower exposure.
- A buy is checked against the account's limits (limits.py), with the settings and a
  snapshot of the account from `accounts` (account.py for paper). It may be blocked,
  or shrunk to fit the coin's cap. Either is recorded in `risk_events`.
- An order whose client order ID was placed before gets the answer it got then.
  Deciding again could block an order that already exists, or shrink it to a
  different one.

Buys are decided one at a time, so two bots can't both take the last position slot.
"""

import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict, JsonValue

from engine.brokers.base import Broker, BrokerOrder, Mode
from engine.brokers.paper import RulesSource
from engine.exchange.models import Balance, OrderRequest
from engine.risk.limits import (
    SELLS_ALLOWED,
    Account,
    Checked,
    RiskDecision,
    RiskEvent,
    RiskSettings,
    check_buy,
)
from engine.strategies.base import Position

log = structlog.get_logger()


class Accounts(Protocol):
    """Where a buy's limits come from: the settings, the account now, and the record of
    what they blocked or shrank (account.py)."""

    @property
    def mode(self) -> Mode: ...

    async def settings(self) -> RiskSettings: ...

    async def snapshot(self, symbol: str) -> Account: ...

    async def record(self, event: RiskEvent) -> None: ...


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskOutcome(_Frozen):
    decision: RiskDecision
    order: BrokerOrder | None  # None when blocked: nothing reached the broker


class RiskManager:
    def __init__(self, broker: Broker, accounts: Accounts, rules: RulesSource) -> None:
        if accounts.mode != broker.mode:
            raise ValueError(
                f"a {broker.mode} broker can't be checked against {accounts.mode} limits"
            )
        self._broker = broker
        self._accounts = accounts
        self._rules = rules
        self._buying = asyncio.Lock()  # one buy at a time, from snapshot to placed

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
        # A sell changes no limit's answer for anyone, so it never waits for a buy.
        turn: AbstractAsyncContextManager[object] = (
            self._buying if request.side == "buy" else nullcontext()
        )
        async with turn:
            existing = await self._broker.get_order(request.client_order_id)
            if existing is not None:
                return await self._repeat(bot_id, request, existing)
            checked, inputs = await self._decide(request)
            decision = checked.decision
            # Logged before the broker is called, so a crash there still leaves the decision.
            log.info(
                "risk.decision",
                mode=self.mode,
                bot_id=bot_id,
                **(checked.request or request).model_dump(mode="json"),
                reason=reason,
                **decision.model_dump(mode="json", exclude_none=True),
                **inputs,
            )
            if decision.decision != "allowed":
                inputs = {"request": request.model_dump(mode="json"), **inputs}
                await self._accounts.record(
                    RiskEvent(bot_id, request.client_order_id, decision, inputs)
                )
            if checked.request is None:
                return RiskOutcome(decision=decision, order=None)
            order = await self._broker.place_order(
                bot_id,
                checked.request,
                {**reason, "risk": decision.model_dump(mode="json", exclude_none=True)},
            )
            return RiskOutcome(decision=decision, order=order)

    async def cancel(self, client_order_id: str) -> BrokerOrder:
        """Cancel what's left of an order. Never blocked: cancelling only lowers risk, and
        the kill switch (P4-5) relies on it always working."""
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

    async def _decide(self, request: OrderRequest) -> tuple[Checked, dict[str, JsonValue]]:
        """The decision, and the inputs it was made on (none for a sell)."""
        if request.side == "sell":
            return Checked(SELLS_ALLOWED, request), {}
        settings = await self._accounts.settings()
        account = await self._accounts.snapshot(request.symbol)
        rules = await self._rules.market_rules(request.symbol)
        inputs: dict[str, JsonValue] = {
            "settings": settings.model_dump(mode="json"),
            "account": account.to_json(),
        }
        return check_buy(settings, account, request, rules), inputs

    async def _repeat(
        self, bot_id: int, request: OrderRequest, existing: BrokerOrder
    ) -> RiskOutcome:
        """The first answer for a client order ID placed before (D35)."""
        decision = RiskDecision.model_validate(existing.reason["risk"])
        asked = request
        if decision.requested_amount == request.amount:
            # It was shrunk: the order placed then is the smaller one.
            asked = request.model_copy(update={"amount": existing.amount})
        # The broker returns the stored order, or raises DuplicateClientOrderId if the
        # ID belongs to a different one.
        order = await self._broker.place_order(bot_id, asked, existing.reason)
        log.info(
            "risk.repeat",
            mode=self.mode,
            bot_id=bot_id,
            client_order_id=request.client_order_id,
            status=order.status,
            **decision.model_dump(mode="json", exclude_none=True),
        )
        return RiskOutcome(decision=decision, order=order)
