# Exchange fixtures

Raw exchange JSON that the adapter tests replay through CCXT, so no test touches the network (see `../conftest.py`).

## `mexc/`

| File | Endpoint | Source |
|---|---|---|
| `exchange_info.json` | `GET /api/v3/exchangeInfo` (BTCUSDT, ETHUSDT) | Recorded live |
| `contract_detail.json` | `GET /api/v1/contract/detail` | Written by the recorder: shape only, empty list (we trade spot only) |
| `klines_btcusdt_60m.json` | `GET /api/v3/klines` | Recorded live (the last candle was still open) |
| `ticker_24hr_btcusdt.json` | `GET /api/v3/ticker/24hr` | Recorded live |
| `account.json` | `GET /api/v3/account` | MEXC API docs example, values changed by hand |
| `order_new.json` | `POST /api/v3/order` | MEXC API docs example, values changed by hand |
| `order_cancel.json` | `DELETE /api/v3/order` | MEXC API docs example, values changed by hand |
| `error_insufficient_balance.json` | HTTP 400 body | Error code 30004 from MEXC's docs |
| `error_bad_signature.json` | HTTP 400 body | Error code 700002 from MEXC's docs |

## `binance_testnet/`

Binance's spot testnet (`testnet.binance.vision`, fake money), the planned P9 test step for LiveBroker (ARCHITECTURE D23).

| File | Endpoint | Source |
|---|---|---|
| `exchange_info.json` | `GET /api/v3/exchangeInfo` (BTCUSDT) | Recorded live from the testnet |

## Re-recording

`uv run python -m engine.exchange.record mexc` (or `binance_testnet`) re-records that exchange's live files. The tests check exact recorded values, so update them to match afterwards. Private endpoints need an API key, so their fixtures are kept by hand; if a real response ever differs, replace the docs example with it.
