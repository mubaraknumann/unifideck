"""Only three places may start an Xodus binary.

``xodus-cli`` and ``xodus-service`` read the user's Xbox credentials, and
the plugin reaches them only through the verified backend
(``launcher/xbox_pc/backend.check_backend``, never PATH). Every spawn goes
through one of:

* ``stores/microsoft/xodus/cli.py``: every ``xodus-cli`` call of the plugin;
* ``stores/microsoft/xodus/update_service.py``: the short ``xodus-service``
  session of the update check;
* ``launcher/xbox_pc/``: the game launch, out of process.

A new module that both spawns a process and names an Xodus binary (or its
argv builders, or the verified backend paths) fails this test, and so
does any spawn elsewhere under ``stores/microsoft/``.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "py_modules" / "unifideck"

ALLOWED_FILES = frozenset({
    "stores/microsoft/xodus/cli.py",
    "stores/microsoft/xodus/update_service.py",
})
ALLOWED_DIRS = ("launcher/xbox_pc/",)

_SPAWN_ATTRS = frozenset({
    "create_subprocess_exec", "create_subprocess_shell",
    "Popen", "run", "call", "check_call", "check_output",
    "execv", "execve", "execvp", "execvpe", "execl", "execlp",
    "spawnv", "spawnve", "spawnvp", "posix_spawn", "posix_spawnp",
})
_SPAWN_OWNERS = frozenset({"asyncio", "subprocess", "os"})
_XODUS_MARKERS = (
    "xodus-cli", "xodus-service", "CLI_NAME", "SERVICE_NAME",
    "BackendPaths", "resolve_backend", "check_backend", "xbox_pc.argv",
    "account_argv", "profile_argv", "login_argv", "logout_argv", "library_argv",
    "resolve_package_argv", "streaming_argv", "run_argv", "service_argv",
)


def _rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def _allowed(rel: str) -> bool:
    return rel in ALLOWED_FILES or rel.startswith(ALLOWED_DIRS)


def _spawns(tree: ast.AST) -> bool:
    """Whether the module calls ``asyncio``/``subprocess``/``os`` spawn functions."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if node.func.attr in _SPAWN_ATTRS and isinstance(owner, ast.Name) and owner.id in _SPAWN_OWNERS:
            return True
    return False


def _modules() -> list[tuple[str, str, ast.AST]]:
    found = []
    for path in sorted(PKG.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found.append((_rel(path), source, ast.parse(source)))
    return found


def test_only_the_allowed_modules_spawn_xodus_binaries() -> None:
    offenders = [
        rel for rel, source, tree in _modules()
        if not _allowed(rel) and _spawns(tree) and any(m in source for m in _XODUS_MARKERS)
    ]
    assert offenders == [], f"spawn an Xodus binary only from {sorted(ALLOWED_FILES)} or {ALLOWED_DIRS}"


def test_nothing_else_under_the_microsoft_store_spawns() -> None:
    offenders = [
        rel for rel, _source, tree in _modules()
        if rel.startswith("stores/microsoft/") and not _allowed(rel) and _spawns(tree)
    ]
    assert offenders == []


def test_main_does_not_spawn_xodus() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert not (_spawns(ast.parse(source)) and any(m in source for m in _XODUS_MARKERS))


def test_the_allowlist_is_not_stale() -> None:
    """Each allowed file exists and really spawns, so the list can only shrink."""
    by_rel = {rel: tree for rel, _source, tree in _modules()}
    for rel in ALLOWED_FILES:
        assert rel in by_rel, rel
        assert _spawns(by_rel[rel]), rel
