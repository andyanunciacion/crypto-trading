"""Brokers: where an allowed order becomes a fill (ARCHITECTURE §7, D34).

`PaperBroker` simulates fills against live prices; `LiveBroker` (P9) will use the
exchange. Both implement `Broker`, and only the RiskManager may call them (D8).
"""
