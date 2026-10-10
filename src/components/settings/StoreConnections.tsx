/**
 * StoreConnections — one row per store, plus an "All stores" row.
 *
 * Each row shows the store's icon (white when signed in, grey when not —
 * the only signed-out cue, the status text stays blank), its localised
 * name and one line of status text, and expands to
 * full-width actions: Sync games, Sync images, Open store, Sign in/out.
 * "All stores" expands to Sync all games, Sync all images and Cancel.
 *
 * A sign-in waiting on the user (the Microsoft device-code flow) shows
 * its code beneath the store's row whether or not the row is open.
 *
 * Status comes from the per-store rows the backend reports on
 * `get_sync_progress` (`SyncProgress.stores`, `store_summary`,
 * `queued`) and is worded by `storeStatus.ts`. Only one row is open at
 * a time; which one is remembered across QAM close/reopen.
 *
 * Driver hooks (`useStores`, `useStoreAuth`, `useSync`) come from the
 * current architecture so the data plane is unchanged.
 */
import { FC, useEffect, useLayoutEffect, useRef, useState } from "react";
import { PanelSection, Focusable, DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { FaImage, FaLayerGroup, FaSync, FaTimes } from "react-icons/fa";
import { useStores } from "../../contexts/StoreContext";
import { useSync } from "../../contexts/SyncContext";
import { useStoreAuth } from "../../hooks/useStoreAuth";
import { useSyncCooldown } from "../../hooks/useSyncCooldown";
import { hasStorefront } from "../../services/store/StorefrontLauncher";
import { syncStore } from "../../stores/sync-store";
import { AuthDispatcher } from "../../services/auth/AuthDispatcher";
import { usePendingSignIn } from "../../stores/pending-signin-store";
import { StoreIcon } from "../shared/StoreIcon";
import { ExpandableRow } from "./ExpandableRow";
import { StoreAuthButton } from "./StoreAuthButton";
import { StoreStorefrontButton } from "./StoreStorefrontButton";
import { STORE_ROW_CSS } from "./storeConnections.css";
import {
  formatAllStoresStatus,
  formatStoreStatus,
  isQueued,
} from "./storeStatus";
import type { StoreId } from "../../types/api";
import type { StoreSyncState } from "../../types/syncProgress";

// ROW_CONFIG lived here: a per-store map of "if the status is
// `legendary_not_installed`, show `storeConnections.legendaryNotInstalled`".
// It could never fire. `StoreStatus` is a closed union of
// "connected" | "disconnected" | "expired" | "error", so the comparison was
// against a value the type cannot hold, and no backend has ever emitted
// either string. It also covered only 2 of the 3 CLI stores — GOG gained a
// CLITool in the §3.2 pass and never got a row.
//
// Telling the user *which* bundled CLI is missing is worth having: a lost
// exec bit is a real failure mode (`scripts/ensure_executable_bits.py`), and
// since §3.5 a missing gogdl makes GOG unavailable. Rebuilding it needs a
// real reason on the status payload, for all three stores — audit register
// item 50. A mechanism that cannot work is worse than none, so this one goes.

const SMALL_BUTTON = {
  padding: "4px 10px",
  fontSize: 10,
  height: 28,
  width: "fit-content",
  minWidth: "unset",
} as const;

/**
 * A sign-in waiting for the user (the Microsoft device-code flow). The code
 * is already filled in on the page in the auth window; it is shown here
 * because the QAM renders above that window, and the window may have been
 * closed. The backend keeps waiting until the code expires either way.
 */
const PendingSignInRow: FC<{ storeId: StoreId }> = ({ storeId }) => {
  const { t } = useTranslation();
  const pending = usePendingSignIn(storeId);
  if (!pending) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 6,
        padding: "2px 0 4px 26px",
      }}
    >
      <span style={{ fontSize: 11, color: "#b8bcbf" }}>
        {t("auth.microsoft.code")}{" "}
        <bdi style={{ fontFamily: "monospace", color: "#fff" }}>
          {pending.userCode}
        </bdi>
      </span>
      <div style={{ display: "flex", gap: 6, flex: "0 0 auto" }}>
        {!pending.windowOpen && (
          <DialogButton
            style={SMALL_BUTTON}
            onClick={() => void AuthDispatcher.reopenWindow(storeId)}
          >
            {t("auth.microsoft.reopen")}
          </DialogButton>
        )}
        <DialogButton
          style={SMALL_BUTTON}
          onClick={() => void AuthDispatcher.cancel(storeId)}
        >
          {t("common.cancel")}
        </DialogButton>
      </div>
    </div>
  );
};

/**
 * Listed after the storefronts. GameVault is a self-hosted server rather
 * than a store, so it goes last instead of in alphabetical order.
 */
const LISTED_LAST: readonly StoreId[] = ["gamevault"];

/** Key of the "All stores" row in the open-row state. */
const ALL_STORES_ROW = "__all__";

/** How long "Done" / "Cancelled" shows before the row goes back to idle. */
const FINISHED_VISIBLE_MS = 4000;

/** States in which a store is part of the run still in progress. */
const IN_RUN_STATES: readonly StoreSyncState[] = [
  "queued",
  "active",
  "waiting",
];

/**
 * The nearest ancestor that actually scrolls — the Quick Access panel's
 * scroll container, which belongs to Steam rather than to us.
 */
function scrollParent(el: HTMLElement | null): HTMLElement | null {
  for (let node = el?.parentElement; node; node = node.parentElement) {
    const { overflowY } = window.getComputedStyle(node);
    if (
      (overflowY === "auto" || overflowY === "scroll") &&
      node.scrollHeight > node.clientHeight
    ) {
      return node;
    }
  }
  return null;
}

/** The open row, kept across QAM mount/unmount like the active tab. */
let persistentExpanded: string | null = null;

/**
 * True for a few seconds after `state` turns `done` / `cancelled`.
 *
 * Only a transition counts: a row that was already finished when the QAM
 * opened shows its idle summary, not a stale "Done".
 */
function useRecentlyFinished(state: StoreSyncState | undefined): boolean {
  const [recent, setRecent] = useState(false);
  const previous = useRef(state);
  useEffect(() => {
    const was = previous.current;
    previous.current = state;
    if (state !== "done" && state !== "cancelled") {
      setRecent(false);
      return undefined;
    }
    if (was === state) return undefined;
    setRecent(true);
    const id = window.setTimeout(() => setRecent(false), FINISHED_VISIBLE_MS);
    return () => window.clearTimeout(id);
  }, [state]);
  return recent;
}

interface RowProps {
  expanded: boolean;
  onToggle: (row: HTMLElement | null) => void;
  /** Cooldown after a run ends, shared by every sync button. */
  canSync: boolean;
}

const StoreRow: FC<RowProps & { storeId: StoreId; displayName: string }> = ({
  storeId,
  displayName,
  expanded,
  onToggle,
  canSync,
}) => {
  const { t } = useTranslation();
  const { status, busy, connect, disconnect } = useStoreAuth(storeId);
  const sync = useSync();
  const isConnected = status === "connected";
  const row = sync.progress?.stores?.[storeId];
  const recentlyFinished = useRecentlyFinished(row?.state);
  const queued = isQueued(sync.progress?.queued, storeId);
  const line = formatStoreStatus(t, {
    connected: isConnected,
    row,
    summary: sync.progress?.store_summary?.[storeId],
    queued,
    recentlyFinished,
  });
  // Another store's run does not block this row: a press queues behind it.
  const inRun =
    sync.isSyncing && row !== undefined && IN_RUN_STATES.includes(row.state);
  const syncDisabled = inRun || queued || !canSync;

  return (
    <>
      <ExpandableRow
        icon={
          <StoreIcon
            store={storeId}
            size="18px"
            color={isConnected ? "#fff" : "#8b929a"}
          />
        }
        label={displayName}
        status={line}
        expanded={expanded}
        onToggle={onToggle}
      >
        {isConnected && (
          <>
            <DialogButton
              className="unifideck-store-action"
              disabled={syncDisabled}
              onClick={() => void sync.syncGames([storeId])}
            >
              <FaSync />
              {t("storeConnections.syncGames")}
            </DialogButton>
            <DialogButton
              className="unifideck-store-action"
              disabled={syncDisabled}
              onClick={() => void sync.syncImages([storeId])}
            >
              <FaImage />
              {t("storeConnections.syncImages")}
            </DialogButton>
          </>
        )}
        {hasStorefront(storeId) && (
          <StoreStorefrontButton
            store={storeId}
            isConnected={isConnected}
            busy={busy}
          />
        )}
        <StoreAuthButton
          store={storeId}
          status={status ?? "disconnected"}
          busy={busy}
          onConnect={() => void connect()}
          onDisconnect={() => void disconnect()}
        />
      </ExpandableRow>
      <PendingSignInRow storeId={storeId} />
    </>
  );
};

const AllStoresRow: FC<RowProps> = ({ expanded, onToggle, canSync }) => {
  const { t } = useTranslation();
  const sync = useSync();
  const progress = sync.progress;
  const line = formatAllStoresStatus(
    t,
    progress?.stores,
    progress?.store_summary,
    progress?.queued,
    sync.isSyncing,
  );
  // Everything is already queued; another press would add nothing.
  const disabled = !canSync || progress?.queued?.stores === null;

  return (
    <ExpandableRow
      icon={<FaLayerGroup size={18} />}
      label={t("storeConnections.allStores")}
      status={line}
      expanded={expanded}
      onToggle={onToggle}
    >
      <DialogButton
        className="unifideck-store-action"
        disabled={disabled}
        onClick={() => void sync.syncGames()}
      >
        <FaSync />
        {t("storeConnections.syncAllGames")}
      </DialogButton>
      <DialogButton
        className="unifideck-store-action"
        disabled={disabled}
        onClick={() => void sync.syncImages()}
      >
        <FaImage />
        {t("storeConnections.syncAllImages")}
      </DialogButton>
      {sync.isSyncing && (
        <DialogButton
          className="unifideck-store-action"
          disabled={sync.isCancelling}
          onClick={() => void sync.cancelSync()}
        >
          <FaTimes />
          {sync.isCancelling
            ? t("storeConnections.cancelling")
            : t("storeConnections.cancelSync")}
        </DialogButton>
      )}
    </ExpandableRow>
  );
};

export const StoreConnections: FC = () => {
  const { t } = useTranslation();
  const { stores, loading } = useStores();
  const cooldown = useSyncCooldown();
  const [expanded, setExpanded] = useState<string | null>(persistentExpanded);

  // The status poll only runs while a sync does, so read the idle game
  // counts and sync times once when the panel opens.
  useEffect(() => {
    void syncStore.refresh();
  }, []);

  // The tapped row and where its header sat on screen just before the
  // toggle. Only one row is open at a time, so opening one can close
  // another above it and pull the tapped header upward; the layout effect
  // below scrolls by however far it moved, so what was tapped stays under
  // the finger. Nothing else ever scrolls the panel on open — the content
  // grows downward, and Steam brings a button into view when the D-pad
  // reaches it.
  const anchor = useRef<{ row: HTMLElement; top: number } | null>(null);

  useLayoutEffect(() => {
    const held = anchor.current;
    anchor.current = null;
    if (!held || !held.row.isConnected) return;
    const moved = held.row.getBoundingClientRect().top - held.top;
    const scroller = scrollParent(held.row);
    if (moved !== 0 && scroller) scroller.scrollTop += moved;
  }, [expanded]);

  const toggle = (id: string, row: HTMLElement | null): void => {
    anchor.current = row ? { row, top: row.getBoundingClientRect().top } : null;
    const next = expanded === id ? null : id;
    persistentExpanded = next;
    setExpanded(next);
  };

  if (loading) return null;
  // Stable sort: the backend's order holds within each group.
  const ordered = [...stores].sort(
    (a, b) =>
      Number(LISTED_LAST.includes(a.name)) -
      Number(LISTED_LAST.includes(b.name)),
  );
  return (
    <PanelSection title={t("storeConnections.title")}>
      {/* Rendered once for the whole section, not per row. */}
      <style>{STORE_ROW_CSS}</style>
      {/* One column: each row is a single focus target, and an open row's
          actions sit directly beneath it, so D-pad down walks into them. */}
      <Focusable
        flow-children="column"
        style={{ display: "flex", flexDirection: "column", gap: 2 }}
      >
        {ordered.map((s) => (
          <StoreRow
            key={s.name}
            storeId={s.name}
            displayName={s.display_name}
            expanded={expanded === s.name}
            onToggle={(row) => toggle(s.name, row)}
            canSync={cooldown.canSync}
          />
        ))}
        <AllStoresRow
          expanded={expanded === ALL_STORES_ROW}
          onToggle={(row) => toggle(ALL_STORES_ROW, row)}
          canSync={cooldown.canSync}
        />
      </Focusable>
    </PanelSection>
  );
};
