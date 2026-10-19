# engine

The trading worker, a separate asyncio process that keeps running if the API or dashboard stops.

Contains market data, strategies, the RiskManager, brokers (paper and live), the bot runner and the regime switcher. It's the only part that talks to the exchange, and only through `ExchangeAdapter`.

Every order goes strategy signal → RiskManager → Broker. See `AI_DOCS/ARCHITECTURE.md` §6–8.
