"""Only the RiskManager may reach a broker (CLAUDE.md hard rule, D8, D35).

Outside engine/brokers/ and engine/risk/, code may import the broker's data types (an
order, a fill, the client order ID helper) but never a broker class or module. Code
that can't name a broker can't build or call one, so its only way to place an order
is the RiskManager it was handed.
"""

import ast
import inspect
from pathlib import Path

import pytest

from engine.brokers import base

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "engine" / "src" / "engine"
ALLOWED_PACKAGES = (ENGINE / "brokers", ENGINE / "risk")
# Tests of the brokers and the RiskManager themselves need a real or fake Broker.
EXEMPT_TESTS = ("test_broker_", "test_paper_broker", "test_risk_manager")
PYTHON_PACKAGES = ("backend", "engine", "shared")
BROKERS = "engine.brokers"
BASE = "engine.brokers.base"
# Values and helpers, nothing that can place or cancel an order.
DATA_ONLY = frozenset(
    {
        "BrokerFill",
        "BrokerOrder",
        "BrokerOrderStatus",
        "DuplicateClientOrderId",
        "Mode",
        "UnknownOrder",
        "client_order_id",
    }
)


def _is_brokers(name: str) -> bool:
    return name == BROKERS or name.startswith(BROKERS + ".")


def broker_imports(path: Path) -> list[str]:
    """The imports in `path` that reach past the broker's data types."""
    # Parsing (not grepping) so comments and strings that mention brokers don't count.
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _is_brokers(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [a.name for a in node.names]
            if node.level > 0:  # relative: `from ..brokers import paper`
                bad = names if "brokers" in [*module.split("."), *names] else []
            elif module == BASE:
                bad = [n for n in names if n not in DATA_ONLY]
            elif _is_brokers(module):
                bad = names
            else:  # `from engine import brokers`
                bad = [n for n in names if _is_brokers(f"{module}.{n}")]
            found += [f"from {'.' * node.level}{module} import {n}" for n in bad]
    return found


def _checked(path: Path) -> bool:
    if any(path.is_relative_to(p) for p in ALLOWED_PACKAGES):
        return False
    return not path.name.startswith(EXEMPT_TESTS)


def test_only_the_risk_manager_reaches_a_broker() -> None:
    files = [p for package in PYTHON_PACKAGES for p in (REPO / package).rglob("*.py")]
    assert files, "found no Python files: is REPO right?"
    offenders = {
        p.relative_to(REPO).as_posix(): found
        for p in files
        if _checked(p) and (found := broker_imports(p))
    }
    assert offenders == {}


def test_the_data_only_names_cannot_place_orders() -> None:
    # Guards the allowlist itself: adding Broker (or a subclass) to it would open the gate.
    for name in DATA_ONLY:
        value = getattr(base, name)
        assert not (inspect.isclass(value) and issubclass(value, base.Broker)), name


@pytest.mark.parametrize(
    "source",
    [
        "from engine.brokers.paper import PaperBroker\n",
        "from engine.brokers.base import Broker\n",
        "from engine.brokers.base import BrokerOrder, Broker\n",
        "from engine.brokers.base import *\n",
        "from engine.brokers.ledger import reserve\n",
        "from engine.brokers import paper\n",
        "from engine import brokers\n",
        "import engine.brokers.paper\n",
        "import engine.brokers\n",
        "from ..brokers import paper\n",
        "from .. import brokers\n",
    ],
)
def test_the_check_would_catch_an_import(tmp_path: Path, source: str) -> None:
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text(source, encoding="utf-8")
    assert broker_imports(sneaky) != []


def test_the_check_allows_the_data_types(tmp_path: Path) -> None:
    fine = tmp_path / "fine.py"
    fine.write_text(
        "from engine.brokers.base import BrokerOrder, client_order_id\n"
        "from engine.risk.manager import RiskManager\n"
        "# from engine.brokers.paper import PaperBroker (a comment)\n",
        encoding="utf-8",
    )
    assert broker_imports(fine) == []
