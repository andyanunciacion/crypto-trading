# Exchange fixtures

Raw exchange JSON that the adapter tests replay through CCXT, so no test touches the network (see `../conftest.py`).

## `mexc/`

| File | Endpoint | Source |
|---|---|---|
| `exchange_info.json` | `GET /api/v3/exchangeInfo` (BTCUSDT, ETHUSDT) | Recorded live |
| `contract_detail.json` | `GET /api/v1/contract/detail` | Written by the recorder: shape only, empty list (we trade spot only) |
| `klines_btcusdt_60m.json` | `GET /api/v3/klines` | Recorded live (the last candle was still open) |
| `ticker_24hr_btcusdt.json` | `GET /api/v3/ticker/24hr` | Recorded live |
| `ws_ohlcv_btcusdt_1m.json` | WebSocket `spot@public.kline.v3.api.pb` (1m), after CCXT decoding | Recorded live: one update of the open candle |
| `live_btcusdt_1m.json.gz` | A whole live candle session: kline updates, the live path's REST requests and replies, and a later re-fetch | Recorded live with `engine.marketdata.record_live` (180 candles); replayed by `test_live_parity.py` |
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

## `binance/`

Binance's public market data (`data-api.binance.vision`, no key). It's history MEXC no longer serves, used by tests only; nothing at runtime talks to Binance.

| File | Endpoint | Source |
|---|---|---|
| `btcusdt_1d_2019_2023.json.gz` | `GET /api/v3/klines` (BTCUSDT, 1d, 2019-06-01 to 2023-12-31), as `Candle` JSON | Recorded live with `engine.regime.record_history` (1,675 days); replayed by `test_regime_history.py` and the P6-5b study (`engine.regime.study`, D56) |
| `ethusdt_1d_2019_2023.json.gz` | The same for ETHUSDT | `record_history --symbol ETH/USDT` (1,675 days); the study's other-coin holdout |
| `btcusdt_1d_2024_2026.json.gz` | The same for BTCUSDT, 2024-01-01 to 2026-09-30 | `record_history --start 2024-01-01 --end 2026-10-01` (1,004 days); the study's later-years holdout |

## Re-recording

`uv run python -m engine.exchange.record mexc` (or `binance_testnet`) re-records that exchange's live files. The tests check exact recorded values, so update them to match afterwards.

`uv run python -m engine.regime.record_history` re-records the Binance daily history (`--symbol`, `--start` and `--end` for the holdouts, as in the table); closed daily candles don't change, so it should give the same file (checked for the BTC 2019-2023 file, 2026-10-29).

`live_btcusdt_1m.json.gz` has its own recorder, which takes about 3 hours. Run it in Docker (see its docstring), and re-record when the live path's REST requests change: the parity test then fails with "re-record". Private endpoints need an API key, so their fixtures are kept by hand; if a real response ever differs, replace the docs example with it.
