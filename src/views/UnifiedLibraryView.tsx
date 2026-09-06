/**
 * UnifiedLibraryView — content for a single Unifideck library tab.
 *
 * Replaces staging's `src/views/UnifiedLibraryView.tsx`. Wired to the
 * RPC layer + LibraryContext rather than to global functions on
 * `useUnifideckGames`. Receives a `filter` prop matching the staging
 * shape so the library-patch hook can render it the same way per tab.
 */
import {
  Component,
  ErrorInfo,
  FC,
  ReactNode,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { useRPCQuery } from "../api/useRPC";
import { rpcRoutes } from "../api/rpc-routes";
import { GameGrid } from "../components/shared/GameGrid";
import { meetsGreatOnCurrentDevice } from "../lib/protondb-cache";
import { resolveCompatForShortcut } from "../lib/library-facets";
import { compatTabTitleKey } from "../lib/device-type";
import type { Game, StoreId } from "../types/api";

export type LibraryFilter = "all" | "installed" | "great-on-deck";

interface UnifiedLibraryViewProps {
  filter: LibraryFilter;
  onSelect?: (game: Game) => void;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
}

class ErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }
  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[Unifideck] UnifiedLibraryView error:", error, info);
  }
  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;
    return (
      <div style={{ padding: 20, color: "#ff6b6b" }}>
        <h3>Error loading library</h3>
        <pre style={{ fontSize: 11, opacity: 0.7 }}>
          {this.state.error?.message}
        </pre>
      </div>
    );
  }
}

function isGreatOnCurrentDevice(game: Game): boolean {
  return meetsGreatOnCurrentDevice(
    resolveCompatForShortcut(game.app_id, game.title),
  );
}

const UnifiedLibraryViewInner: FC<UnifiedLibraryViewProps> = ({
  filter,
  onSelect,
}) => {
  const { t } = useTranslation();
  const { data, error, loading, refetch } = useRPCQuery<[], Game[]>(
    rpcRoutes.getAllUnifideckGames,
    [],
  );
  const [storeFilter, setStoreFilter] = useState<StoreId | "all">("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Re-pull the library when a sync finishes. Without this the view keeps its
  // mount-time snapshot (e.g. 0 games for a store the user logged into AFTER
  // opening the panel) until Steam is restarted. Mirrors the cache refresh in
  // ``src/lib/library-filters/index.ts``.
  useEffect(() => {
    const onSync = (): void => {
      void refetch();
    };
    window.addEventListener("unifideck-sync-completed", onSync);
    return () => window.removeEventListener("unifideck-sync-completed", onSync);
  }, [refetch]);

  const filteredGames = useMemo(() => {
    let games = data ?? [];
    if (filter === "installed") {
      // Raw RPC rows carry ``installed`` (wire field); ``is_installed`` only
      // exists after ``adaptGame`` runs, which it doesn't on this path.
      games = games.filter((g) => g.installed ?? g.is_installed);
    } else if (filter === "great-on-deck") {
      games = games.filter(isGreatOnCurrentDevice);
    }
    if (storeFilter !== "all") {
      games = games.filter((g) => g.store === storeFilter);
    }
    const q = searchQuery.trim().toLowerCase();
    if (q) games = games.filter((g) => g.title.toLowerCase().includes(q));
    return [...games].sort((a, b) => a.title.localeCompare(b.title));
  }, [data, filter, storeFilter, searchQuery]);

  if (error) {
    return (
      <div style={{ padding: 20, color: "#ff6b6b" }}>
        <div style={{ marginBottom: 10, fontSize: 16 }}>
          {t("unifiedLibrary.errorLoadingGames")}
        </div>
        <div style={{ fontSize: 12, opacity: 0.7 }}>{error.message}</div>
      </div>
    );
  }

  const title =
    filter === "installed"
      ? t("deckTabs.installed")
      : filter === "great-on-deck"
      ? // Named after the actual device, same as the injected tab.
        t(compatTabTitleKey())
      : t("deckTabs.allGames");

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div
        style={{
          padding: "15px 20px",
          background: "rgba(0,0,0,0.3)",
          borderBottom: "1px solid rgba(255,255,255,0.1)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 15,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <div style={{ fontSize: 18, fontWeight: "bold" }}>{title}</div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 12, opacity: 0.7 }}>
              {t("unifiedLibrary.storeLabel")}
            </span>
            <select
              value={storeFilter}
              onChange={(e) =>
                setStoreFilter(e.target.value as StoreId | "all")
              }
              style={{
                background: "rgba(255,255,255,0.1)",
                border: "1px solid rgba(255,255,255,0.2)",
                borderRadius: 4,
                padding: "4px 8px",
                color: "white",
                fontSize: 12,
              }}
            >
              <option value="all">{t("unifiedLibrary.allStores")}</option>
              <option value="steam">{t("deckTabs.steam")}</option>
              <option value="epic">{t("deckTabs.epic")}</option>
              <option value="gog">{t("deckTabs.gog")}</option>
              <option value="amazon">{t("deckTabs.amazon")}</option>
              <option value="ubisoft">{t("deckTabs.ubisoft")}</option>
              <option value="battlenet">{t("deckTabs.battlenet")}</option>
              <option value="microsoft">{t("deckTabs.microsoft")}</option>
            </select>
          </div>
          <input
            type="text"
            placeholder={t("unifiedLibrary.searchPlaceholder")}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              background: "rgba(255,255,255,0.1)",
              border: "1px solid rgba(255,255,255,0.2)",
              borderRadius: 4,
              padding: "6px 12px",
              color: "white",
              fontSize: 12,
              flex: 1,
              minWidth: 200,
            }}
          />
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto" }}>
        {loading ? (
          <div style={{ padding: 20, opacity: 0.7 }}>{t("common.loading")}</div>
        ) : (
          <GameGrid games={filteredGames} onSelect={onSelect ?? (() => {})} />
        )}
      </div>
    </div>
  );
};

export const UnifiedLibraryView: FC<UnifiedLibraryViewProps> = (props) => (
  <ErrorBoundary>
    <UnifiedLibraryViewInner {...props} />
  </ErrorBoundary>
);
