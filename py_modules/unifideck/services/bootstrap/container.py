"""services/bootstrap/container.py — Dependency injection container.

Holds typed references to all service instances. Used as the single injection
point — main.py creates one and passes it to RPC handlers, or test harnesses
can create one with a subset of services.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.auth.browser import OAuthBrowserMonitor
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.cdp.cdp_client import CDPClient
    from unifideck.core.metrics_collector import MetricsCollector
    from unifideck.services.account_service import AccountService
    from unifideck.services.achievements import AchievementWatcher
    from unifideck.services.activity_log import ActivityLogService
    from unifideck.services.artwork import ArtworkService
    from unifideck.services.cloud_save import CloudSaveService
    from unifideck.services.compatibility import CompatibilityService
    from unifideck.services.download import DownloadService
    from unifideck.services.feature_flag_service import FeatureFlagService
    from unifideck.services.launch_history import LaunchHistoryService
    from unifideck.services.launch_logs import LaunchLogsService
    from unifideck.services.memory_sampler import MemorySamplerService
    from unifideck.services.metadata_service import MetadataService
    from unifideck.services.microsoft_subscription import MicrosoftSubscriptionService
    from unifideck.services.playtime import PlaytimeService
    from unifideck.services.playtime_sync import PlaytimeSyncService
    from unifideck.services.probe_reaction_service import ProbeReactionService
    from unifideck.services.proton_service import ProtonService
    from unifideck.services.security import SecurityService
    from unifideck.services.shortcut import ShortcutService
    from unifideck.services.support_bundle import SupportBundleService
    from unifideck.services.user_paths_coordinator import UserPathsCoordinator


@dataclass
class ServiceContainer:
    """Dependency injection container holding all service instances."""

    # MemorySamplerService — records this process's own memory footprint on
    # a timer so a support bundle carries a growth curve, not a single
    # reading. Declared first so that, since teardown walks these fields in
    # reverse, it is the last thing stopped and keeps sampling across the
    # rest of the shutdown. Depends on nothing but config.
    memory_sampler: MemorySamplerService | None = None
    shortcut: ShortcutService | None = None
    download: DownloadService | None = None
    metadata: MetadataService | None = None
    artwork: ArtworkService | None = None
    compatibility: CompatibilityService | None = None
    activity_log: ActivityLogService | None = None
    proton: ProtonService | None = None
    cdp: CDPClient | None = None
    cloudsave: CloudSaveService | None = None
    metrics: MetricsCollector | None = None
    account: AccountService | None = None
    playtime: PlaytimeService | None = None
    playtime_sync: PlaytimeSyncService | None = None
    feature_flags: FeatureFlagService | None = None
    probe_reaction: ProbeReactionService | None = None
    security: SecurityService | None = None
    launch_history: LaunchHistoryService | None = None
    launch_logs: LaunchLogsService | None = None
    # SupportBundleService — the "Capture Logs" button. Collects every
    # log + state file into one zip in the user's Downloads folder,
    # audits every path the plugin can touch, and probes the device.
    # Read-only: it describes the install, never repairs it.
    support_bundle: SupportBundleService | None = None
    microsoft_subscription: MicrosoftSubscriptionService | None = None
    # AchievementWatcher — GOG live unlock toasts + end-of-session summary.
    # Reads achievements back from GOG (Comet does the in-game unlocking);
    # plugin-only (the launcher subset never requests it).
    achievements: AchievementWatcher | None = None
    # OAuth browser monitor — shared CDP-based redirect watcher
    # consumed by every store's `AuthOrchestrator`. Injected into
    # stores via `store_injector._STORE_INJECTIONS`.
    browser_monitor: OAuthBrowserMonitor | None = None
    # Edge browser — flatpak installer + CDP launcher used by
    # the four OAuth stores (Epic / GOG / Amazon / Microsoft).
    # Constructed once per plugin and shared via the injector.
    edge_browser: EdgeBrowser | None = None
    # Re-binds per-user paths (shortcuts.vdf / grid / localconfig) onto the
    # shortcut/artwork/proton services when the active Steam user changes —
    # driven by ACCOUNT_SWITCHED and the set_active_steam_user RPC.
    user_paths_coordinator: UserPathsCoordinator | None = None
