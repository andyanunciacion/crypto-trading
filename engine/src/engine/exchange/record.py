"""Re-record the public exchange responses that the adapter tests replay.

Run with `uv run python -m engine.exchange.record mexc` (or `binance_testnet`) when
that exchange or CCXT changes. It calls public endpoints only (no API key) and
overwrites that exchange's recorded files in engine/tests/fixtures/; then update the
values the tests expect. Private responses (account, orders) can't be recorded
without a key, so those fixtures are copied from MEXC's API docs by hand and are
left alone here.
"""

import argparse
import asyncio
import json
import ssl
from pathlib import Path

import ccxt.async_support as ccxt
import ccxt.pro as ccxt_pro

# src/engine/exchange/record.py -> engine/tests/fixtures
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
MEXC_SYMBOLS = ("BTCUSDT", "ETHUSDT")


def _trust_os_certificates(exchange: object) -> None:
    # Trust the OS certificate store instead of CCXT's bundled certifi list, so this
    # works behind antivirus HTTPS scanning (Avast) on a dev PC. Still verified.
    exchange.ssl_context = ssl.create_default_context()  # type: ignore[attr-defined]


async def fetch_mexc() -> dict[str, object]:
    # CCXT's "implicit API" methods return the exchange's raw JSON, before any parsing.
    exchange = ccxt.mexc()
    _trust_os_certificates(exchange)
    try:
        exchange_info = await exchange.spotPublicGetExchangeInfo(
            {"symbols": ",".join(MEXC_SYMBOLS)}
        )
        klines = await exchange.spotPublicGetKlines(
            {"symbol": "BTCUSDT", "interval": "60m", "limit": 3}
        )
        ticker = await exchange.spotPublicGetTicker24hr({"symbol": "BTCUSDT"})
    finally:
        await exchange.close()
    return {
        "mexc/exchange_info.json": exchange_info,
        # load_markets() also asks for futures contracts. We trade spot only, so the
        # fixture keeps the response shape with an empty list.
        "mexc/contract_detail.json": {"success": True, "code": 0, "data": []},
        "mexc/klines_btcusdt_60m.json": klines,
        "mexc/ticker_24hr_btcusdt.json": ticker,
        **await fetch_mexc_stream(),
    }


async def fetch_mexc_stream() -> dict[str, object]:
    # MEXC's WebSocket sends Protobuf, which can't be replayed through a fake socket as
    # easily as JSON. So this records one update after CCXT has decoded and parsed it:
    # the dict the adapter's watch_ticker receives.
    exchange = ccxt_pro.mexc()
    exchange.number = str  # as the adapter sets it, so the numbers are recorded as strings
    _trust_os_certificates(exchange)
    try:
        ticker = await exchange.watch_ticker("BTC/USDT")
        ohlcv = await exchange.watch_ohlcv("BTC/USDT", "1m")
    finally:
        await exchange.close()
    return {"mexc/ws_ticker_btcusdt.json": ticker, "mexc/ws_ohlcv_btcusdt_1m.json": ohlcv}


async def fetch_binance_testnet() -> dict[str, object]:
    exchange = ccxt.binance()
    exchange.set_sandbox_mode(True)  # testnet.binance.vision: fake money (D23)
    _trust_os_certificates(exchange)
    try:
        exchange_info = await exchange.publicGetExchangeInfo({"symbol": "BTCUSDT"})
    finally:
        await exchange.close()
    return {"binance_testnet/exchange_info.json": exchange_info}


RECORDERS = {"mexc": fetch_mexc, "binance_testnet": fetch_binance_testnet}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exchange", choices=sorted(RECORDERS), help="which fixtures to re-record")
    # Recording replaces the numbers the tests expect, so record only what you mean to.
    responses = asyncio.run(RECORDERS[parser.parse_args().exchange]())
    for name, body in responses.items():
        path = FIXTURES_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
