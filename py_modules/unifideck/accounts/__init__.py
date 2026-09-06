"""Steam account-switch detection and data migration.

Ported from ``staging`` (pre-refactor monolith) during the mixin
split. The modal RPCs it depends on were dropped by that split and
have since been restored: ``check_account_switch`` and
``migrate_account_data`` live on
:class:`~unifideck.rpc.mixins.account.AccountRPCMixin`, the frontend
calls them from ``bootstrap-tasks.tsx`` at boot, and
``AccountSwitchModal`` renders the result. Do not read an earlier
version of this docstring as evidence the flow is dead.

This package owns the startup detection and the artwork half of the
migration; shortcut recreation is driven from the mixin via
``ShortcutService.reconcile``. Live switch detection during a session
is a separate concern, handled by ``services/account_service.py``.
See :class:`AccountManager`.
"""

from .account_manager import AccountManager

__all__ = ["AccountManager"]
