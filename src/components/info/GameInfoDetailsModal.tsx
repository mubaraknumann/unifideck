/**
 * GameInfoDetailsModal — per-device compatibility detail view.
 *
 * Opened by the "Details" button in {@link GameInfoCompatRow}.
 * Shows the same colour-coded badge as the row, lists every
 * verification test result Valve published for THIS device (or a
 * "no results" message when none are cached), and offers a "View on
 * ProtonDB" button for games with a real Steam App ID.
 *
 * The title and the test results both follow the device: a Steam
 * Machine owner is shown Machine criteria, not the Deck's panel
 * legibility and APU performance checks.
 */
import { FC } from "react";
import { ConfirmModal, DialogButton } from "@decky/ui";
import { useTranslation } from "react-i18next";
import {
  FaCheckCircle,
  FaExclamationTriangle,
  FaExternalLinkAlt,
} from "react-icons/fa";
import type { GameMetadata } from "../../types/api";
import {
  COMPAT_COLORS,
  asCategory,
  compatLabelKey,
  compatTitleKey,
} from "./CompatBadge";
import { renderTestResults } from "../../lib/compat-tokens";

interface Props {
  meta: GameMetadata;
  closeModal: () => void;
}

export const GameInfoDetailsModal: FC<Props> = ({ meta, closeModal }) => {
  const { t } = useTranslation();
  const track = meta.compat_device;
  const info = meta.compat?.[track];
  const category = asCategory(info?.category);
  const colors = COMPAT_COLORS[category];
  const labelKey = compatLabelKey(category, track);
  const results = renderTestResults(info?.test_results);
  const protonDbUrl = meta.steam_app_id
    ? `https://www.protondb.com/app/${meta.steam_app_id}`
    : null;
  return (
    <ConfirmModal
      strTitle={t(compatTitleKey(track))}
      strDescription=""
      bHideCloseIcon={false}
      onOK={closeModal}
      onCancel={closeModal}
      strOKButtonText={t("gameInfoPanel.compatibility.close")}
    >
      <div style={{ padding: "10px 0" }}>
        <div
          style={{
            marginBottom: 16,
            display: "flex",
            gap: 12,
            alignItems: "center",
          }}
        >
          <span
            style={{
              padding: "4px 12px",
              borderRadius: 4,
              background: colors.bg,
              color: colors.fg,
              fontWeight: 700,
              fontSize: 12,
              letterSpacing: 0.5,
              textTransform: "uppercase",
            }}
          >
            {t(labelKey)}
          </span>
          {meta.title && (
            <span style={{ color: "#c7d5e0", fontSize: 14, fontWeight: 600 }}>
              {meta.title}
            </span>
          )}
        </div>
        {results.length > 0 ? (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {results.map((r, i) => (
              <li
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 0",
                  fontSize: 13,
                  color: "#acb2b8",
                }}
              >
                {r.passed ? (
                  <FaCheckCircle style={{ color: "#59bf40", flexShrink: 0 }} />
                ) : (
                  <FaExclamationTriangle
                    style={{ color: "#ffc82c", flexShrink: 0 }}
                  />
                )}
                <span>{r.text}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div style={{ color: "#8f98a0", fontSize: 13, fontStyle: "italic" }}>
            {t("gameInfoPanel.noTestResults")}
          </div>
        )}
        {protonDbUrl && (
          <div style={{ marginTop: 16 }}>
            <DialogButton
              style={{
                width: "auto",
                flex: "0 0 auto",
                display: "inline-flex",
                alignItems: "center",
                minWidth: 0,
              }}
              onClick={() => {
                window.open(
                  protonDbUrl,
                  "_blank",
                  "width=1024,height=768,popup=yes",
                );
                closeModal();
              }}
            >
              <span
                style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
              >
                <FaExternalLinkAlt />
                {t("gameInfoPanel.compatibility.viewOnProtonDb")}
              </span>
            </DialogButton>
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};
