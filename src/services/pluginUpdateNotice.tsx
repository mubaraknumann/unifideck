/**
 * pluginUpdateNotice — tell the user a new Unifideck release exists.
 *
 * Until this, nothing did. `UpdaterService._poll_loop` on the backend
 * checks every 6 hours and only writes a log line (its docstring claimed
 * it emitted `PLUGIN_UPDATE_AVAILABLE`, but no such event exists
 * anywhere in the repo), so the only way to find out was to open the QAM
 * and read the version header.
 *
 * Two outputs, deliberately with different lifetimes :
 *
 *  - a toast, capped at MAX_NOTICES_PER_VERSION per released version and
 *    at most once per Steam session, so a new release is announced without
 *    turning into a nag;
 *  - the badge flag in `lib/plugin-update.ts`, published on EVERY pass
 *    including the ones that suppress the toast. The dot has to stay lit
 *    for as long as the update is pending — it is the durable signal, the
 *    toast is only the interruption.
 *
 * Frontend-only on purpose: no new RPC, no EventBus event. The backend
 * already serves `check_plugin_update` off a 1-hour cache, so polling it
 * costs nothing, and keeping the frequency cap here keeps it next to the
 * localStorage it needs.
 */
import { call, toaster } from "@decky/api";
import i18n from "i18next";
import { rpcRoutes } from "../api/rpc-routes";
import { unwrapRpcEnvelope } from "../api/useRPC";
import { setUpdatePending, type UpdateCheckResult } from "../lib/plugin-update";

/** Shared origin with the rest of the plugin's persisted UI state
 *  (`unifideck.lang`, `unifideck:view-mode`, `unifideck:collections.*`). */
const STORAGE_KEY = "unifideck:plugin-update-notice";

const MAX_NOTICES_PER_VERSION = 2;
/** Gap enforced between the first and second showing for one version. */
const MIN_GAP_MS = 24 * 60 * 60 * 1000;
/** Let Steam finish booting before competing for the toast area. */
const INITIAL_DELAY_MS = 30_000;
/** Mirrors the backend poller's cadence, for a Deck that sleeps for days
 *  without ever reloading the plugin. */
const RECHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

interface NoticeRecord {
  /** The release this record is about; a different one re-arms the cap. */
  version: string;
  shown: number;
  lastShownAt: number;
}

/** All localStorage access is guarded — it throws in some CEF contexts,
 *  and a quota/availability error must never cost us the badge. */
function readRecord(): NoticeRecord | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<NoticeRecord>;
    if (typeof parsed?.version !== "string") return null;
    return {
      version: parsed.version,
      shown: typeof parsed.shown === "number" ? parsed.shown : 0,
      lastShownAt:
        typeof parsed.lastShownAt === "number" ? parsed.lastShownAt : 0,
    };
  } catch {
    return null;
  }
}

function writeRecord(record: NoticeRecord): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch {
    // Ignore quota/availability errors — worst case the user sees the
    // toast once more than intended, which beats losing the notice.
  }
}

/** Whether this pass is allowed to interrupt the user. */
function shouldToast(record: NoticeRecord, now: number): boolean {
  if (record.shown >= MAX_NOTICES_PER_VERSION) return false;
  if (record.shown >= 1 && now - record.lastShownAt < MIN_GAP_MS) return false;
  return true;
}

function showNotice(version: string): void {
  toaster.toast({
    title: i18n.t("updater.noticeTitle", {
      defaultValue: "Unifideck Update Available",
    }),
    body: i18n.t("updater.noticeBody", {
      version,
      defaultValue: `v${version} is ready to install from the Unifideck settings panel.`,
    }),
    duration: 8000,
    // No onClick: this is informational. The badge is what persists, and
    // the QAM settings panel is where the user acts on it.
  });
}

async function runCheck(): Promise<void> {
  let result: UpdateCheckResult;
  try {
    const raw = await call<[], unknown>(rpcRoutes.checkPluginUpdate);
    result = unwrapRpcEnvelope<UpdateCheckResult>(raw, {
      route: rpcRoutes.checkPluginUpdate,
      throwing: false,
    });
  } catch {
    return; // best-effort — never let this surface an error to the user
  }

  // A local `./build-plugin.sh dev` build stamps dev_build.json, so
  // current_build_id is non-null. Dev builds are cut BEFORE package.json's
  // version bumps, which means a dev Deck running newer code than the
  // latest tag would otherwise be nagged to "update" backwards onto it.
  const isDevBuild = result?.current_build_id !== null;
  const latestVersion = result?.latest?.version;
  const pending = Boolean(result?.available) && !isDevBuild && !!latestVersion;

  // Published on every pass, toast or not — see the module docstring.
  setUpdatePending(pending);
  if (!pending || !latestVersion) return;

  const now = Date.now();
  const stored = readRecord();
  const record: NoticeRecord =
    stored && stored.version === latestVersion
      ? stored
      : { version: latestVersion, shown: 0, lastShownAt: 0 };

  if (!shouldToast(record, now)) return;

  showNotice(latestVersion);
  writeRecord({
    version: latestVersion,
    shown: record.shown + 1,
    lastShownAt: now,
  });
}

/**
 * Start the update-notice loop. Returns a disposer that must be called
 * on plugin unload — an orphaned 6-hour interval surviving a dev-cycle
 * reload is exactly the phantom-listener class `teardown.ts` guards
 * against.
 */
export function startPluginUpdateNotice(): () => void {
  let stopped = false;

  const initialTimer = setTimeout(() => {
    if (!stopped) void runCheck();
  }, INITIAL_DELAY_MS);

  const recheckTimer = setInterval(() => {
    if (!stopped) void runCheck();
  }, RECHECK_INTERVAL_MS);

  return () => {
    stopped = true;
    clearTimeout(initialTimer);
    clearInterval(recheckTimer);
  };
}
