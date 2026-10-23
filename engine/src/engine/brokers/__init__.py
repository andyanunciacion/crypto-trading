"""Brokers: where an allowed order becomes a fill (ARCHITECTURE §7, D34).

`PaperBroker` simulates fills against live prices; `LiveBroker` (P9) will use the
exchange. Both implement `Broker`, and only the RiskManager (`engine.risk`) may call
them (D8, D35): outside these two packages, code may import `base`'s data types only,
which test_broker_boundary.py checks.
"""
