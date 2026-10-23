"""Risk: the one way to a broker (ARCHITECTURE §6, D8, D35).

`RiskManager` sits between signals and the broker and decides on every order. Until
P4 it is a pass-through stub that logs each one. Nothing outside `engine.brokers` and
`engine.risk` may import a broker (test_broker_boundary.py).
"""
