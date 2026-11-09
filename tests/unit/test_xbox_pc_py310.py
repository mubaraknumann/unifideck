"""The launcher-side Xbox PC modules must run on the system Python 3.10.

ruff targets py311 and mypy targets 3.11, so neither would flag
``hashlib.file_digest``, ``asyncio.timeout`` or ``except*``. The launcher
runs these modules under SteamOS's, Bazzite's or CachyOS's own
``/usr/bin/python3``, which can be 3.10. A 3.11 API there is a crash at
launch on that device and a pass everywhere else. These tests are the only
gate that sees it.

Three checks over every module under ``launcher/xbox_pc/`` plus
``file_lock.py``:

* the source parses with ``feature_version=(3, 10)`` (catches syntax such as
  ``except*``);
* no denylisted 3.11+ API name appears;
* every import is stdlib or one of the launcher-safe prefixes.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "py_modules" / "unifideck"
MODULES = [
    *sorted((PKG / "launcher" / "xbox_pc").rglob("*.py")),
    PKG / "launcher" / "proton" / "infrastructure" / "file_lock.py",
]

#: 3.11+ APIs, and ``asyncio.wait_for`` whose ``TimeoutError`` type differs
#: on 3.10 (bounded waits use ``asyncio.wait`` instead).
DENYLIST = (
    "asyncio.timeout",
    "TaskGroup",
    "StrEnum",
    "datetime.UTC",
    "typing.Self",
    "tomllib",
    "file_digest(",
    "except*",
    ".wait_for(",
    "ExceptionGroup",
)

#: Non-stdlib imports a launcher module may make.
ALLOWED_PREFIXES = (
    "unifideck.launcher.",
    "unifideck.core.types",
    "unifideck.core.binaries.cli_env",
)


def _ids(paths: list[Path]) -> list[str]:
    return [p.relative_to(PKG).as_posix() for p in paths]


def test_the_module_list_is_not_empty() -> None:
    """A glob that matched nothing would make every other test pass."""
    names = {p.name for p in MODULES}
    assert {"layout.py", "argv.py", "file_lock.py"} <= names


@pytest.mark.parametrize("path", MODULES, ids=_ids(MODULES))
def test_parses_as_python_310(path: Path) -> None:
    """Syntax newer than 3.10 fails here."""
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def _code_only(source: str) -> str:
    """The source with docstrings and comments removed.

    The denylist is about code. A docstring that explains why
    ``hashlib.file_digest`` is avoided must not trip it.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                body[0] = ast.Pass()
    return ast.unparse(tree)


@pytest.mark.parametrize("path", MODULES, ids=_ids(MODULES))
def test_uses_no_311_api(path: Path) -> None:
    """No 3.11+ name, and no ``wait_for``, in the code."""
    code = _code_only(path.read_text(encoding="utf-8"))
    found = [token for token in DENYLIST if token in code]
    assert found == []


def test_code_only_strips_docstrings() -> None:
    """The stripping above is what keeps the denylist honest."""
    src = '"""Avoid hashlib.file_digest."""\nx = 1\n'
    assert "file_digest" not in _code_only(src)
    assert "file_digest(" in _code_only("import hashlib\nhashlib.file_digest(f, 's')\n")


def _imports(tree: ast.AST) -> list[str]:
    """Absolute module names imported by ``tree`` (relative ones are ours)."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _allowed(name: str) -> bool:
    if name == "__future__":
        return True
    if name.split(".", 1)[0] in sys.stdlib_module_names:
        return True
    return any(name == p.rstrip(".") or name.startswith(p) for p in ALLOWED_PREFIXES)


@pytest.mark.parametrize("path", MODULES, ids=_ids(MODULES))
def test_imports_only_stdlib_and_launcher_safe_modules(path: Path) -> None:
    """A store or service import would drag ``cryptography`` into the launcher."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = [name for name in _imports(tree) if not _allowed(name)]
    assert bad == []


def test_relative_imports_stay_inside_the_package() -> None:
    """``from .x import`` may not climb out of ``launcher/xbox_pc``."""
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 1:
                pytest.fail(f"{path.name} climbs out with level {node.level}")
