"""The Settings page's exchange keys (D50): list, add, replace and delete.

On the `protected` router (app.py), so a session is checked first.

- `GET /exchange-accounts`: each account's exchange, label, sandbox flag and the API
  key's last 4 characters. Never the key or the secret (store.py).
- `POST /exchange-accounts`: add one. Needs a TOTP `code` (keys are what P9 trades real
  money with), checked in the transaction that stores it; without a right one it's a
  403 and nothing is stored. A label in use is a 409, checked before the code, so the
  code isn't used up on it.
- `PUT /exchange-accounts/{id}`: replace its key and secret (e.g. a key MEXC expired),
  with a code as above.
- `DELETE /exchange-accounts/{id}`: no code: forgetting a key only lowers risk.

The key and secret are `SecretStr`, so they show as `**********` if printed, and no
422 echoes what was sent (app.py), so they never come back to the browser. They're
never logged: the logs name the account and its hint.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Self, cast

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.datastructures import State

from backend.auth.confirm import CodeRefused, code_refusal
from backend.auth.crypto import Secrets
from backend.auth.deps import CurrentUser
from backend.keys.store import (
    AccountView,
    Exchange,
    add,
    exists,
    label_taken,
    read_all,
    remove,
    replace_keys,
)

log = structlog.get_logger()

router = APIRouter(tags=["settings"])

MIN_LENGTH = 16  # so the 4-character hint is a small part of the key
MAX_LENGTH = 256


def _a_credential(value: SecretStr) -> SecretStr:
    # The messages never include the value: it's a secret.
    plain = value.get_secret_value()
    if not MIN_LENGTH <= len(plain) <= MAX_LENGTH:
        raise ValueError(f"must be {MIN_LENGTH} to {MAX_LENGTH} characters")
    if any(c.isspace() for c in plain):
        raise ValueError("must not contain spaces or line breaks (a copy-paste slip?)")
    return value


Credential = Annotated[SecretStr, AfterValidator(_a_credential)]


class Keys(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: Credential
    api_secret: Credential
    code: str | None = Field(default=None, max_length=16)  # TOTP; required


class NewAccount(Keys):
    exchange: Exchange
    label: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    sandbox: bool = False

    @model_validator(mode="after")
    def _sandbox_exists(self) -> Self:
        if self.exchange == "mexc" and self.sandbox:
            raise ValueError("MEXC has no sandbox: paper mode is how to test on it")
        return self


def _now(state: State) -> datetime:
    return cast(Callable[[], datetime], state.clock)()


def _secrets(state: State) -> Secrets:
    return cast(Secrets, state.secrets)


@router.get("/exchange-accounts")
async def accounts(request: Request) -> list[AccountView]:
    return await read_all(request.app.state.db)


@router.post("/exchange-accounts", status_code=status.HTTP_201_CREATED)
async def create(body: NewAccount, request: Request, user: CurrentUser) -> AccountView:
    state = request.app.state
    now = _now(state)
    refused: CodeRefused | None = None
    async with state.db.begin() as conn:
        if await label_taken(conn, body.label):
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=f"an account called {body.label!r} exists"
            )
        refused = await code_refusal(
            conn, _secrets(state), user.id, body.code, now, needed_for="saving exchange keys"
        )
        if refused is None:
            account = await add(
                conn,
                _secrets(state),
                exchange=body.exchange,
                label=body.label,
                api_key=body.api_key.get_secret_value(),
                api_secret=body.api_secret.get_secret_value(),
                sandbox=body.sandbox,
                now=now,
            )
    fields = {"user": user.username, "exchange": body.exchange, "label": body.label}
    if refused is not None:
        log.warning("api.exchange_account.refused", why=refused.detail, **fields)
        raise refused  # once the wrong code's failure is committed
    log.info("api.exchange_account.added", id=account.id, key_hint=account.key_hint, **fields)
    return account


@router.put("/exchange-accounts/{account_id}")
async def replace(account_id: int, body: Keys, request: Request, user: CurrentUser) -> AccountView:
    state = request.app.state
    now = _now(state)
    refused: CodeRefused | None = None
    async with state.db.begin() as conn:
        if not await exists(conn, account_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such exchange account")
        refused = await code_refusal(
            conn, _secrets(state), user.id, body.code, now, needed_for="saving exchange keys"
        )
        if refused is None:
            account = await replace_keys(
                conn,
                _secrets(state),
                account_id,
                api_key=body.api_key.get_secret_value(),
                api_secret=body.api_secret.get_secret_value(),
                now=now,
            )
    if refused is not None:
        log.warning(
            "api.exchange_account.refused", why=refused.detail, id=account_id, user=user.username
        )
        raise refused
    log.info(
        "api.exchange_account.replaced",
        id=account.id,
        exchange=account.exchange,
        label=account.label,
        key_hint=account.key_hint,
        user=user.username,
    )
    return account


@router.delete("/exchange-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(account_id: int, request: Request, user: CurrentUser) -> Response:
    account = await remove(request.app.state.db, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such exchange account")
    log.info(
        "api.exchange_account.deleted",
        id=account.id,
        exchange=account.exchange,
        label=account.label,
        key_hint=account.key_hint,
        user=user.username,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
