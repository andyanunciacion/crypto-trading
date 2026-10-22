"""Only the exchange adapter package may import CCXT (CLAUDE.md hard rule)."""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADAPTER_PACKAGE = REPO / "engine" / "src" / "engine" / "exchange"
PYTHON_PACKAGES = ("backend", "engine", "shared")


def imports_ccxt(path: Path) -> bool:
    # Parsing (not grepping) so comments and strings that mention ccxt don't count.
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == "ccxt" or name.startswith("ccxt.") for name in names):
            return True
    return False


def test_only_the_adapter_imports_ccxt() -> None:
    files = [p for package in PYTHON_PACKAGES for p in (REPO / package).rglob("*.py")]
    assert files, "found no Python files: is REPO right?"
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in files
        if not p.is_relative_to(ADAPTER_PACKAGE) and imports_ccxt(p)
    ]
    assert offenders == []


def test_the_check_would_catch_an_import(tmp_path: Path) -> None:
    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text("from ccxt.async_support import mexc\n", encoding="utf-8")
    assert imports_ccxt(sneaky)
