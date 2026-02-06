import React, { useState, useEffect, useRef } from "react";
import { DialogButton, Focusable, showModal, ConfirmModal } from "@decky/ui";
import { call, toaster } from "@decky/api";
import { useTranslation } from "react-i18next";
import { StoreFinal } from "../types/store";
import StoreIcon from "./StoreIcon";
import { UninstallConfirmModal } from "./UninstallConfirmModal";
import { GOGLanguageSelectModal } from "./GOGLanguageSelectModal";
import { updateSingleGameStatus } from "../tabs";

// Steam Deck compatibility categories
enum ESteamDeckCompatibilityCategory {
  Unknown = 0,
  Unsupported = 1,
  Playable = 2,
  Verified = 3,
}

// Compatibility badge colors matching Steam's design
const COMPAT_COLORS: Record<
  ESteamDeckCompatibilityCategory,
  { bg: string; text: string }
> = {
  [ESteamDeckCompatibilityCategory.Verified]: {
    bg: "#59bf40",
    text: "#ffffff",
  },
  [ESteamDeckCompatibilityCategory.Playable]: {
    bg: "#ffc82c",
    text: "#000000",
  },
  [ESteamDeckCompatibilityCategory.Unsupported]: {
    bg: "#ff4444",
    text: "#ffffff",
  },
  [ESteamDeckCompatibilityCategory.Unknown]: { bg: "#666666", text: "#ffffff" },
};

// Support URLs per store
const SUPPORT_URLS: Record<StoreFinal | "steam" | "other", string> = {
  epic: "https://www.epicgames.com/help/assistant",
  gog: "https://support.gog.com/hc/en-us?product=gog",
  amazon:
    "https://www.amazon.in/gp/help/customer/display.html?nodeId=GA5ZHN5T2JX8UGF7",
  ubisoft: "https://www.ubisoft.com/en-us/help",
  ea: "https://help.ea.com/en/",
  battlenet: "https://us.battle.net/support/en/",
  itch: "https://itch.io/support",
  steam: "", // Will be filled with appId
  other: "", // Fallback
};

interface DeckTestResult {
  text: string;
  passed: boolean;
}

interface GameMetadata {
  steamAppId: number;
  hasSteamStorePage: boolean; // true only when steamAppId is a real Steam App ID (not SteamGridDB)
  store: StoreFinal;
  storeUrl: string;
  title: string;
  developer: string;
  publisher: string;
  releaseDate: string;
  metacritic: number | null; // Metacritic score
  description: string;
  deckCompatibility: ESteamDeckCompatibilityCategory;
  deckTestResults: DeckTestResult[];
  genres: string[];
  homepageUrl?: string;
}

interface GameInfoPanelProps {
  appId: number;
}

/**
 * GameInfoPanel - Displays metadata for non-Steam games
 * Matches Steam's GAME INFO tab layout with functional navigation buttons
 */
const GameInfoPanel: React.FC<GameInfoPanelProps> = ({ appId }) => {
  const { t } = useTranslation();
  const [metadata, setMetadata] = useState<GameMetadata | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  // Install button state
  const [gameInfo, setGameInfo] = useState<any>(null);
  const [processing, setProcessing] = useState(false);
  const [downloadState, setDownloadState] = useState<{
    isDownloading: boolean;
    progress?: number;
    downloadId?: string;
  }>({ isDownloading: false });
  const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchMetadata = async () => {
      console.log(
        `[Unifideck GameInfoPanel] Fetching metadata for appId: ${appId}`,
      );
      try {
        setLoading(true);
        setError(null);

        const result = await call<[number], GameMetadata | null>(
          "get_game_metadata_display",
          appId,
        );

        console.log(`[Unifideck GameInfoPanel] Backend result:`, result);

        if (!cancelled) {
          if (result) {
            setMetadata(result);
            console.log(
              `[Unifideck GameInfoPanel] Metadata set for ${result.title}`,
            );
          } else {
            setError("No metadata available");
            console.log(`[Unifideck GameInfoPanel] No metadata returned`);
          }
          setLoading(false);
        }
      } catch (err) {
        console.error(
          "[Unifideck GameInfoPanel] Error fetching metadata:",
          err,
        );
        if (!cancelled) {
          setError("Failed to load metadata");
          setLoading(false);
        }
      }
    };

    fetchMetadata();

    return () => {
      cancelled = true;
    };
  }, [appId]);

  // Fetch game info for install button
  useEffect(() => {
    call<[number], any>("get_game_info", appId)
      .then((info) => {
        setGameInfo(info?.error ? null : info);
      })
      .catch(() => setGameInfo(null));
  }, [appId]);

  // Poll for download state
  useEffect(() => {
    if (!gameInfo) return;

    const checkDownloadState = async () => {
      try {
        const result = await call<
          [string, string],
          {
            success: boolean;
            is_downloading: boolean;
            download_info?: {
              id: string;
              progress_percent: number;
              status: string;
            };
          }
        >("is_game_downloading", gameInfo.game_id, gameInfo.store);

        setDownloadState((prevState) => {
          const newState = {
            isDownloading: false,
            progress: 0,
            downloadId: undefined as string | undefined,
          };

          if (result.success && result.is_downloading && result.download_info) {
            const status = result.download_info.status;
            if (status === "downloading" || status === "queued") {
              newState.isDownloading = true;
              newState.progress = result.download_info.progress_percent;
              newState.downloadId = result.download_info.id;
            }
          }

          // Detect completion
          if (prevState.isDownloading && !newState.isDownloading) {
            const finalStatus = result.download_info?.status;
            if (finalStatus === "completed") {
              toaster.toast({
                title: t("toasts.installComplete"),
                body: t("toasts.installCompleteMessage", {
                  title: gameInfo?.title || "Game",
                }),
                duration: 10000,
                critical: true,
              });

              // Refresh game info
              call<[number], any>("get_game_info", appId).then((info) => {
                const processedInfo = info?.error ? null : info;
                setGameInfo(processedInfo);
                if (processedInfo) {
                  updateSingleGameStatus({
                    appId,
                    store: processedInfo.store,
                    isInstalled: processedInfo.is_installed,
                  });
                }
              });
            }
          }

          return newState;
        });
      } catch (error) {
        console.error("[GameInfoPanel] Error checking download state:", error);
      }
    };

    checkDownloadState();
    pollIntervalRef.current = setInterval(checkDownloadState, 1000);

    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, [gameInfo, appId, t]);

  // Start download with optional language (for GOG games)
  const startDownload = async (language?: string) => {
    if (!gameInfo) return;
    setProcessing(true);

    // Use add_to_download_queue directly with language parameter
    const result = await call<
      [string, string, string, boolean, string | null],
      any
    >(
      "add_to_download_queue",
      gameInfo.game_id,
      gameInfo.title,
      gameInfo.store,
      gameInfo.is_installed || false,
      language || null
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadStarted"),
        body: t("toasts.downloadQueued", { title: gameInfo.title }),
        duration: 5000,
      });

      setDownloadState((prev) => ({
        ...prev,
        isDownloading: true,
        progress: 0,
      }));
    } else {
      toaster.toast({
        title: t("toasts.downloadFailed"),
        body: result.error
          ? t(result.error)
          : t("toasts.downloadFailedMessage"),
        duration: 10000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  // Install handler - checks for GOG language selection
  const handleInstall = async () => {
    if (!gameInfo) return;

    // For GOG games, check if multiple languages are available
    if (gameInfo.store === "gog") {
      setProcessing(true); // Show loading state while fetching languages
      try {
        const langResult = await call<
          [string],
          { success: boolean; languages: string[]; error?: string }
        >("get_gog_game_languages", gameInfo.game_id);

        setProcessing(false); // Clear loading before showing modal

        // Validate response - handle null/undefined/malformed responses
        const languages = langResult?.languages;
        if (!langResult?.success || !Array.isArray(languages)) {
          console.warn(
            "[GameInfoPanel] Invalid language response, falling back to default:",
            langResult?.error || "unknown error"
          );
          startDownload();
          return;
        }

        // Multiple languages - show selection modal
        if (languages.length > 1) {
          showModal(
            <GOGLanguageSelectModal
              gameTitle={gameInfo.title}
              languages={languages}
              onConfirm={(selectedLang) => startDownload(selectedLang)}
            />
          );
          return;
        }

        // Single or no language - use first available or fallback
        startDownload(languages[0] || undefined);
        return;
      } catch (error) {
        setProcessing(false); // Clear loading on error
        console.error("[GameInfoPanel] Error fetching GOG languages:", error);
        // Fallback to download without language selection
      }
    }

    // Non-GOG games or fallback - download without language
    startDownload();
  };

  const handleCancel = async () => {
    const dlId =
      downloadState.downloadId || `${gameInfo.store}:${gameInfo.game_id}`;
    setProcessing(true);

    const result = await call<[string], { success: boolean; error?: string }>(
      "cancel_download_by_id",
      dlId,
    );

    if (result.success) {
      toaster.toast({
        title: t("toasts.downloadCancelled"),
        body: t("toasts.downloadCancelledMessage", { title: gameInfo?.title }),
        duration: 5000,
      });
      setDownloadState({ isDownloading: false, progress: 0 });
    } else {
      toaster.toast({
        title: t("toasts.cancelFailed"),
        body: result.error ? t(result.error) : t("toasts.cancelFailedMessage"),
        duration: 5000,
        critical: true,
      });
    }
    setProcessing(false);
  };

  const handleUninstall = async (deletePrefix: boolean = false) => {
    if (!gameInfo) return;
    setProcessing(true);

    toaster.toast({
      title: t("toasts.uninstalling"),
      body: deletePrefix
        ? t("toasts.uninstallingMessageProton", { title: gameInfo.title })
        : t("toasts.uninstallingMessage", { title: gameInfo.title }),
      duration: 5000,
    });

    const result = await call<[number, boolean], any>(
      "uninstall_game_by_appid",
      appId,
      deletePrefix,
    );

    if (result.success) {
      setGameInfo({ ...gameInfo, is_installed: false });

      if (result.game_update) {
        updateSingleGameStatus(result.game_update);
      }

      toaster.toast({
        title: t("toasts.uninstallComplete"),
        body: deletePrefix
          ? t("toasts.uninstallCompleteMessageProton", {
              title: gameInfo.title,
            })
          : t("toasts.uninstallCompleteMessage", { title: gameInfo.title }),
        duration: 10000,
      });
    }
    setProcessing(false);
  };

  const showInstallConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.installTitle")}
        strDescription={t("confirmModals.installDescription", {
          title: gameInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        onOK={() => {
          handleInstall();
          return true; // Return true to dismiss modal
        }}
      />,
    );
  };

  const showUninstallConfirmation = () => {
    showModal(
      <UninstallConfirmModal
        gameTitle={gameInfo?.title || "this game"}
        onConfirm={(deletePrefix) => handleUninstall(deletePrefix)}
      />,
    );
  };

  const showCancelConfirmation = () => {
    showModal(
      <ConfirmModal
        strTitle={t("confirmModals.cancelTitle")}
        strDescription={t("confirmModals.cancelDescription", {
          title: gameInfo?.title,
        })}
        strOKButtonText={t("confirmModals.yes")}
        strCancelButtonText={t("confirmModals.no")}
        bDestructiveWarning={true}
        onOK={() => handleCancel()}
      />,
    );
  };

  // Open URL in browser popup (same method as auth flow)
  const openUrl = (url: string) => {
    if (url) {
      window.open(url, "_blank", "width=1024,height=768,popup=yes");
    }
  };

  // Generate Steam URLs using steam:// protocol for native Steam navigation
  const getSteamUrl = (type: string) => {
    if (!metadata || metadata.steamAppId <= 0) return "";
    const id = metadata.steamAppId;
    switch (type) {
      case "store":
        return `steam://store/${id}`;
      case "dlc":
        return `steam://openurl/https://store.steampowered.com/dlc/${id}`;
      case "community":
        return `steam://url/GameHub/${id}`;
      case "points":
        return `steam://openurl/https://store.steampowered.com/points/shop/app/${id}`;
      case "discussions":
        return `steam://openurl/https://steamcommunity.com/app/${id}/discussions/`;
      case "guides":
        return `steam://openurl/https://steamcommunity.com/app/${id}/guides/`;
      case "support":
        return `steam://openurl/https://help.steampowered.com/en/wizard/HelpWithGame/?appid=${id}`;
      default:
        return "";
    }
  };

  // Get support URL based on store
  const getSupportUrl = () => {
    if (!metadata) return "";
    const storeUrl = SUPPORT_URLS[metadata.store];
    if (storeUrl) return storeUrl;
    // Fallback to Steam support with mapped appId
    return getSteamUrl("support");
  };

  // CSS for controller focus state (.gpfocus is automatically applied by Steam)
  const focusStyles = `
    .unifideck-nav-button.gpfocus,
    .unifideck-nav-button:hover {
      filter: brightness(1.3) !important;
      background-color: rgba(255, 255, 255, 0.2) !important;
    }
    
    /* Install button - blue (always) */
    .unifideck-install-button.install-state {
      background-color: #1a9fff !important;
    }
    
    /* Install button - brighter blue when focused */
    .unifideck-install-button.install-state.gpfocus,
    .unifideck-install-button.install-state:hover {
      background-color: #1a9fff !important;
      filter: brightness(1.2) !important;
    }
    
    /* Uninstall button - red (always) */
    .unifideck-install-button.uninstall-state {
      background-color: #d32f2f !important;
    }
    
    /* Uninstall button - brighter red when focused */
    .unifideck-install-button.uninstall-state.gpfocus,
    .unifideck-install-button.uninstall-state:hover {
      background-color: #d32f2f !important;
      filter: brightness(1.2) !important;
    }
    
    /* Cancel button - red (always) */
    .unifideck-install-button.cancel-state {
      background-color: #d32f2f !important;
    }
  `;

  // Button styles matching Steam's bottom bar - prevent stretching
  const buttonStyle: React.CSSProperties = {
    padding: "8px 16px",
    fontSize: "13px",
    fontWeight: 500,
    backgroundColor: "rgba(255, 255, 255, 0.1)",
    color: "#ffffff",
    border: "none",
    borderRadius: "4px",
    cursor: "pointer",
    display: "inline-flex",
    alignItems: "center",
    gap: "6px",
    minWidth: "auto",
    width: "fit-content",
    flex: "none",
  };

  const containerStyle: React.CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "16px",
    padding: "16px",
    backgroundColor: "rgba(0, 0, 0, 0.2)",
    borderRadius: "8px",
  };

  const buttonRowStyle: React.CSSProperties = {
    display: "flex",
    flexWrap: "wrap",
    gap: "8px",
    alignItems: "center",
  };

  // Show Details modal with Valve test results and ProtonDB link
  const showDetailsModal = (meta: GameMetadata) => {
    const protonDbUrl = `https://www.protondb.com/app/${meta.steamAppId}`;

    showModal(
      <ConfirmModal
        strTitle={t("gameInfoPanel.compatibility.modalTitle")}
        strDescription=""
        strOKButtonText={t("gameInfoPanel.compatibility.viewOnProtonDb")}
        strCancelButtonText={t("gameInfoPanel.compatibility.close")}
        onOK={() => meta.steamAppId > 0 && openUrl(protonDbUrl)}
        onCancel={() => {}}
      >
        <div style={{ padding: "10px 0" }}>
          {/* Compatibility Status */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "12px",
              marginBottom: "16px",
            }}
          >
            <span style={{ color: "#c7d5e0", fontSize: "14px" }}>
              {t("gameInfoPanel.labels.status")}
            </span>
            <div
              style={{
                backgroundColor: COMPAT_COLORS[meta.deckCompatibility].bg,
                color: COMPAT_COLORS[meta.deckCompatibility].text,
                padding: "4px 12px",
                borderRadius: "4px",
                fontWeight: 700,
                fontSize: "12px",
              }}
            >
              {getCompatLabel(meta.deckCompatibility)}
            </div>
          </div>

          {/* Test Results */}
          {meta.deckTestResults && meta.deckTestResults.length > 0 ? (
            <div
              style={{
                backgroundColor: "rgba(255,255,255,0.05)",
                padding: "15px",
                borderRadius: "8px",
              }}
            >
              {meta.deckTestResults.map((result, index) => (
                <div
                  key={index}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "10px",
                    marginBottom:
                      index < meta.deckTestResults.length - 1 ? "10px" : 0,
                  }}
                >
                  <span
                    style={{
                      color: result.passed ? "#59bf40" : "#ffc82c",
                      fontSize: "16px",
                      flexShrink: 0,
                    }}
                  >
                    {result.passed ? "✓" : "⚠"}
                  </span>
                  <span style={{ color: "#acb2b8", fontSize: "13px" }}>
                    {result.text}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div
              style={{
                color: "#8f98a0",
                fontSize: "13px",
              }}
            >
              {t("gameInfoPanel.noTestResults")}
            </div>
          )}
        </div>
      </ConfirmModal>,
    );
  };

  // Helper to get localized compat labels
  const getCompatLabel = (category: ESteamDeckCompatibilityCategory) => {
    const labels: Record<ESteamDeckCompatibilityCategory, string> = {
      [ESteamDeckCompatibilityCategory.Verified]: t(
        "gameInfoPanel.compatibility.verified",
      ),
      [ESteamDeckCompatibilityCategory.Playable]: t(
        "gameInfoPanel.compatibility.playable",
      ),
      [ESteamDeckCompatibilityCategory.Unsupported]: t(
        "gameInfoPanel.compatibility.unsupported",
      ),
      [ESteamDeckCompatibilityCategory.Unknown]: t(
        "gameInfoPanel.compatibility.unknown",
      ),
    };
    return labels[category];
  };

  if (loading) {
    return (
      <div style={containerStyle}>
        <div style={{ color: "#8f98a0", fontSize: "14px" }}>
          {t("gameInfoPanel.loading")}
        </div>
      </div>
    );
  }

  if (error || !metadata) {
    return (
      <div style={containerStyle}>
        <div style={{ color: "#8f98a0", fontSize: "14px" }}>
          {error || t("gameInfoPanel.noMetadata")}
        </div>
      </div>
    );
  }

  const compatColor = COMPAT_COLORS[metadata.deckCompatibility];
  const compatLabel = getCompatLabel(metadata.deckCompatibility);
  const hasValidSteamId = metadata.hasSteamStorePage;

  return (
    <div style={containerStyle}>
      {/* Steam Deck Compatibility Section - Focusable row for gamepad nav */}
      <style>{focusStyles}</style>
      <Focusable
        style={{ display: "flex", alignItems: "center", gap: "12px" }}
        flow-children="row"
      >
        {/* Compatibility Badge */}
        <div
          style={{
            backgroundColor: compatColor.bg,
            color: compatColor.text,
            padding: "4px 12px",
            borderRadius: "4px",
            fontWeight: 700,
            fontSize: "12px",
            letterSpacing: "0.5px",
          }}
        >
          {compatLabel}
        </div>

        {/* Details Button - Opens modal with test results */}
        <DialogButton
          onClick={() => showDetailsModal(metadata)}
          style={{
            ...buttonStyle,
            padding: "4px 12px",
            fontSize: "12px",
          }}
          className="unifideck-nav-button"
        >
          {t("gameInfoPanel.buttons.details")}
        </DialogButton>

        {/* Synopsis Button - toggles description, turns blue when active */}
        {metadata.description && (
          <DialogButton
            onClick={() => setExpanded(!expanded)}
            style={{
              ...buttonStyle,
              padding: "4px 12px",
              fontSize: "12px",
              ...(expanded
                ? {
                    backgroundColor: "#1a9fff",
                    color: "#ffffff",
                  }
                : {}),
            }}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.synopsis")}
          </DialogButton>
        )}

        {/* Install/Uninstall/Cancel Button */}
        {gameInfo && !gameInfo.error && (
          <DialogButton
            onClick={
              processing
                ? undefined
                : downloadState.isDownloading
                ? showCancelConfirmation
                : gameInfo.is_installed
                ? showUninstallConfirmation
                : showInstallConfirmation
            }
            disabled={processing}
            style={{
              ...buttonStyle,
              padding: "4px 12px",
              fontSize: "12px",
              display: "flex",
              alignItems: "center",
              gap: "6px",
              opacity: processing ? 0.5 : 1,
            }}
            className={`unifideck-nav-button unifideck-install-button ${
              downloadState.isDownloading
                ? "cancel-state"
                : gameInfo.is_installed
                ? "uninstall-state"
                : "install-state"
            }`}
          >
            <span>
              {processing
                ? "..."
                : downloadState.isDownloading
                ? `${t("gameInfoPanel.buttons.cancel")} (${
                    downloadState.progress || 0
                  }%)`
                : gameInfo.is_installed
                ? t("gameInfoPanel.buttons.uninstall")
                : t("gameInfoPanel.buttons.install")}
            </span>
          </DialogButton>
        )}

        {/* Genre Tags */}
        {metadata.genres && metadata.genres.length > 0 && (
          <span style={{ color: "#8f98a0", fontSize: "13px" }}>
            {metadata.genres.join(" • ")}
          </span>
        )}
      </Focusable>

      {/* Game Info Row */}
      {(gameInfo ||
        metadata.developer ||
        metadata.publisher ||
        metadata.releaseDate ||
        metadata.metacritic) && (
        <div
          style={{
            backgroundColor: "rgba(0, 0, 0, 0.3)",
            borderRadius: "6px",
            padding: "12px 16px",
            marginTop: "4px",
          }}
        >
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "16px",
              fontSize: "13px",
              color: "#8f98a0",
              alignItems: "center",
            }}
          >
            {/* Store Logo */}
            {gameInfo && metadata.store && (
              <div style={{ display: "flex", alignItems: "center" }}>
                <StoreIcon store={metadata.store} size="20px" />
              </div>
            )}

            {/* Game Name */}
            {gameInfo?.title && (
              <div style={{ fontWeight: 600, color: "#c7d5e0" }}>
                {gameInfo.title}
              </div>
            )}

            {/* Game Size */}
            {gameInfo?.size_formatted && (
              <div>
                <span style={{ fontWeight: 600, color: "#c7d5e0" }}>
                  {t("gameInfoPanel.labels.size")}{" "}
                </span>
                {gameInfo.size_formatted}
              </div>
            )}

            {metadata.developer && (
              <div>
                <span style={{ fontWeight: 600, color: "#c7d5e0" }}>
                  {t("gameInfoPanel.labels.developer")}{" "}
                </span>
                {metadata.developer}
              </div>
            )}
            {metadata.publisher && (
              <div>
                <span style={{ fontWeight: 600, color: "#c7d5e0" }}>
                  {t("gameInfoPanel.labels.publisher")}{" "}
                </span>
                {metadata.publisher}
              </div>
            )}
            {metadata.releaseDate && (
              <div>
                <span style={{ fontWeight: 600, color: "#c7d5e0" }}>
                  {t("gameInfoPanel.labels.released")}{" "}
                </span>
                {metadata.releaseDate}
              </div>
            )}
            {metadata.metacritic && (
              <div
                style={{ display: "flex", alignItems: "center", gap: "6px" }}
              >
                <span style={{ fontWeight: 600, color: "#c7d5e0" }}>
                  {t("gameInfoPanel.labels.metacritic")}
                </span>
                <span
                  style={{
                    backgroundColor:
                      metadata.metacritic >= 75
                        ? "#66cc33"
                        : metadata.metacritic >= 50
                        ? "#ffcc33"
                        : "#ff0000",
                    color: "#000",
                    padding: "2px 8px",
                    borderRadius: "3px",
                    fontWeight: 700,
                    fontSize: "12px",
                  }}
                >
                  {metadata.metacritic}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Synopsis Expanded Description */}
      {expanded && metadata.description && (
        <div
          style={{
            backgroundColor: "rgba(0, 0, 0, 0.3)",
            borderRadius: "6px",
            padding: "12px 16px",
            marginTop: "4px",
            fontSize: "14px",
            color: "#acb2b8",
            lineHeight: "1.5",
          }}
        >
          {metadata.description}
        </div>
      )}

      {/* Navigation Buttons Row */}
      <Focusable style={buttonRowStyle} flow-children="row">
        {/* Store Page - uses steam:// when available, falls back to store URL */}
        <DialogButton
          onClick={() =>
            openUrl(hasValidSteamId ? getSteamUrl("store") : metadata.storeUrl)
          }
          style={buttonStyle}
          className="unifideck-nav-button"
        >
          {t("gameInfoPanel.buttons.storePage")}
        </DialogButton>

        {/* DLC - only when game exists on Steam */}
        {hasValidSteamId && (
          <DialogButton
            onClick={() => openUrl(getSteamUrl("dlc"))}
            style={buttonStyle}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.dlc")}
          </DialogButton>
        )}

        {/* Community Hub - only when game exists on Steam */}
        {hasValidSteamId && (
          <DialogButton
            onClick={() => openUrl(getSteamUrl("community"))}
            style={buttonStyle}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.communityHub")}
          </DialogButton>
        )}

        {/* Points Shop - only when game exists on Steam */}
        {hasValidSteamId && (
          <DialogButton
            onClick={() => openUrl(getSteamUrl("points"))}
            style={buttonStyle}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.pointsShop")}
          </DialogButton>
        )}

        {/* Discussions - only when game exists on Steam */}
        {hasValidSteamId && (
          <DialogButton
            onClick={() => openUrl(getSteamUrl("discussions"))}
            style={buttonStyle}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.discussions")}
          </DialogButton>
        )}

        {/* Guides - only when game exists on Steam */}
        {hasValidSteamId && (
          <DialogButton
            onClick={() => openUrl(getSteamUrl("guides"))}
            style={buttonStyle}
            className="unifideck-nav-button"
          >
            {t("gameInfoPanel.buttons.guides")}
          </DialogButton>
        )}

        {/* Support - always visible, uses store-specific URLs */}
        <DialogButton
          onClick={() => openUrl(getSupportUrl())}
          style={buttonStyle}
          className="unifideck-nav-button"
        >
          {t("gameInfoPanel.buttons.support")}
        </DialogButton>
      </Focusable>
    </div>
  );
};

export default GameInfoPanel;
