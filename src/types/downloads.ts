/**
 * Download queue DTOs.
 *
 * Mirrors the backend `services/download/models.py`. The
 * `DownloadQueueInfo` shape is what the `get_download_queue`
 * RPC returns; the queue snapshot is reactive on the frontend
 * via `DownloadContext` (Phase F2).
 */
import type { Result, StoreId } from "./api";

/**
 * High-level state of a download item. Mirrors the values
 * emitted by ``DownloadItem.status`` in the backend
 * (``"queued"`` → ``"running"`` → ``"complete"`` / ``"failed"``
 * / ``"cancelled"``). Keep these strings in lock-step with
 * the worker — silent mismatches make the UI sit on the
 * wrong label or hide speed/ETA.
 */
export type DownloadStatus =
  | "queued"
  | "running"
  | "complete"
  | "failed"
  | "cancelled";

/**
 * Sub-status used while `status === "running"` to show
 * what the underlying CLI is currently doing.
 */
export type DownloadPhase =
  | "downloading"
  | "extracting"
  | "verifying"
  | "complete"
  // Launcher-driven install (Ubisoft Connect): the external launcher
  // performs the install, so there is no %/speed/ETA. Rendered as an
  // indeterminate "Installing in Ubisoft Connect" state.
  | "manual"
  // Post-download prefix setup (Epic/GOG/Amazon): after the files land we
  // run the full first-run prefix init (createprefix + redistributables) and
  // pull cloud saves before completing. No %/speed/ETA — rendered as an
  // indeterminate "Setting up game…" state.
  | "preparing";

/**
 * Where the game install lives. `internal` = eMMC, `custom` =
 * user-picked path, `sdcard` = legacy removable-media alias kept
 * for configs saved before external mounts got unique ids. Every
 * currently-mounted external device gets its own `ext:<name>` id
 * (see `mounts.mount_id` on the backend) so two simultaneously
 * mounted drives (e.g. an SD card and a USB drive) are distinct,
 * selectable locations rather than colliding on one shared id.
 */
export type StorageLocation = "internal" | "sdcard" | "custom" | (string & {});

/**
 * One row of the download queue — kept loosely coupled to the
 * backend `DownloadItem` dataclass so the wire shape stays
 * snake_case / camelCase neutral.
 */
export interface DownloadItem {
  id: string;
  game_id: string;
  game_title: string;
  store: StoreId;
  status: DownloadStatus;
  progress_percent: number;
  downloaded_bytes: number;
  total_bytes: number;
  speed_mbps: number;
  eta_seconds: number;
  added_time: number;
  storage_location: StorageLocation;
  start_time?: number;
  end_time?: number;
  error_message?: string;
  download_phase?: DownloadPhase;
  /** A wrapper store's explanation of why nothing is moving (e.g. Battle.net
   *  "queued behind the updater"). Sent by the backend, **not yet rendered** —
   *  it is English, so displaying it needs an i18n decision. Audit item 49. */
  phase_message?: string;
  /** True when this entry is an update of an already-installed
   *  game (enqueued via `update_game`), false for a fresh install.
   *  Drives the "Downloading Update" / "Update Queued" label. */
  is_update?: boolean;
}

/**
 * Snapshot returned by `get_download_queue` : current item,
 * pending queue, finished history, and overall queue state.
 */
export interface DownloadQueueInfo extends Result {
  current: DownloadItem | null;
  queued: DownloadItem[];
  finished: DownloadItem[];
  state: "idle" | "running";
}

/**
 * Description of one storage option offered to the user when
 * picking where to install : path, free space, label.
 */
export interface StorageLocationInfo {
  id: StorageLocation;
  label: string;
  path: string;
  /** Device root this location sits on — the mount point for an
   *  external drive. Used by the backend to recognise ids saved under
   *  an older scheme; the UI has no reason to read it. */
  device_path?: string;
  available: boolean;
  free_space_gb: number;
}

/**
 * Wrapped response of `get_storage_locations`. The wrapper
 * carries the typed `Result` outcome alongside the data so
 * callers don't need a second flag for errors.
 */
export interface StorageLocationsResponse extends Result {
  locations: StorageLocationInfo[];
  default: StorageLocation;
}

/**
 * On-disk facts about one installed game, for the Downloads tab's
 * meta line. `location` is `null` when the install directory could
 * not be stat'd (moved / unmounted drive) — the UI then shows the
 * size alone rather than guessing a location.
 */
export interface InstalledDiskInfo {
  size_bytes: number;
  location: "internal" | "external" | null;
}

/**
 * Response of `get_installed_disk_info` : one entry per installed
 * game, keyed `"<store>:<store_game_id>"` — the same key the
 * shortcut cache uses (`resolveAppIdFromStoreGame`), NOT `Game.id`.
 * Games whose size and location both failed to resolve are absent.
 */
export type InstalledDiskInfoMap = Record<string, InstalledDiskInfo>;

/**
 * Compact flag returned by `is_downloading` to tell the UI
 * whether a download is in progress without paying for the
 * full queue snapshot.
 */
export interface IsDownloadingResponse extends Result {
  is_downloading: boolean;
  download_info?: DownloadItem;
}
