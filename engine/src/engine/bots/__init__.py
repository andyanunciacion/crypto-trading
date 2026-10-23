"""Bots: a strategy config running on one symbol and timeframe (ARCHITECTURE §7, D36).

`store` loads the running bots from the database, `orders` turns a signal into an order,
and `runner` runs each bot on every closed candle, trading through the RiskManager.
"""
