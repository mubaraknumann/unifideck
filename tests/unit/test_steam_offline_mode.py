"""Steam offline mode read from loginusers.vdf (launcher/steam_offline.py)."""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.steam_offline import steam_offline_mode


def _users(root: Path, *blocks: tuple[str, str, str]) -> Path:
    """``(steamid, MostRecent, WantsOfflineMode)`` per account."""
    body = "".join(
        f'\t"{sid}"\n\t{{\n\t\t"AccountName"\t\t"a"\n\t\t"MostRecent"\t\t"{recent}"\n'
        f'\t\t"WantsOfflineMode"\t\t"{offline}"\n\t\t"Timestamp"\t\t"1761900000"\n\t}}\n'
        for sid, recent, offline in blocks
    )
    (root / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").write_text(f'"users"\n{{\n{body}}}\n')
    return root


def test_offline_mode_of_the_signed_in_account(tmp_path: Path) -> None:
    root = _users(tmp_path, ("76561198000000001", "1", "1"))
    assert steam_offline_mode(root)


def test_online_account_with_ones_elsewhere_in_the_file_is_online(tmp_path: Path) -> None:
    root = _users(tmp_path, ("76561198000000001", "1", "0"))
    assert not steam_offline_mode(root)


def test_another_account_left_offline_does_not_count(tmp_path: Path) -> None:
    root = _users(tmp_path, ("76561198000000001", "0", "1"), ("76561198000000002", "1", "0"))
    assert not steam_offline_mode(root)


def test_missing_file_means_online(tmp_path: Path) -> None:
    assert not steam_offline_mode(tmp_path)
