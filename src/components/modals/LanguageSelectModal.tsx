/**
 * LanguageSelectModal — language picker for multi-language
 * installs.
 *
 * Games can ship their languages as separate downloads. When the
 * backend's `get_gog_game_languages` (GOG) or
 * `get_install_languages` (Epic Selective Downloads titles, EA
 * builds) returns more than one option, the install flow defers to this
 * modal so the user picks before the download is queued. With one
 * language available the modal is skipped entirely (queued with
 * the default).
 *
 * Store-neutral by design: it takes an option list and hands back
 * the picked value verbatim, so a new store only has to report its
 * languages. Epic passes `labels` since its SDL configs carry their
 * own display names.
 *
 * Pure presentational : the actual install RPC is the caller's
 * responsibility — this component only collects the choice.
 */
import { FC, useState } from "react";
import { ConfirmModal, Dropdown, DropdownOption } from "@decky/ui";
import { useTranslation } from "react-i18next";
import i18n from "i18next";
import { pickDefaultLanguage } from "../../lib/i18n/pick-default-language";

/** Display labels for bare locale codes, which is what GOG reports.
 *  Falls back to the raw code if one isn't recognised — the modal
 *  still works, just without the localised name. Stores that ship
 *  their own labels pass them via `labels` instead. */
const LANGUAGE_NAMES: Record<string, string> = {
  "en-US": "English",
  "de-DE": "Deutsch (German)",
  "fr-FR": "Français (French)",
  "es-ES": "Español (Spanish)",
  "it-IT": "Italiano (Italian)",
  "pt-BR": "Português (Brasil)",
  "ru-RU": "Русский (Russian)",
  "pl-PL": "Polski (Polish)",
  "zh-CN": "简体中文 (Simplified Chinese)",
  "zh-Hans": "简体中文 (Simplified Chinese)",
  "zh-TW": "繁體中文 (Traditional Chinese)",
  "ja-JP": "日本語 (Japanese)",
  "ko-KR": "한국어 (Korean)",
  "nl-NL": "Nederlands (Dutch)",
  "tr-TR": "Türkçe (Turkish)",
  "uk-UA": "Українська (Ukrainian)",
  "cs-CZ": "Čeština (Czech)",
  "hu-HU": "Magyar (Hungarian)",
  "sv-SE": "Svenska (Swedish)",
  "da-DK": "Dansk (Danish)",
  "fi-FI": "Suomi (Finnish)",
  "no-NO": "Norsk (Norwegian)",
  "ar-SA": "العربية (Arabic)",
  "th-TH": "ไทย (Thai)",
};

interface Props {
  gameTitle: string;
  languages: string[];
  onConfirm: (language: string) => void;
  closeModal?: () => void;
  /** Locale tag to pre-select if the game offers it. Defaults to
   *  the active UI language (which reflects the "auto" preference
   *  resolved to the system language). */
  preferredTag?: string;
  /** Per-option display names, consulted ahead of {@link LANGUAGE_NAMES}.
   *  Epic's SDL configs ship their own labels (e.g. `zh-Hans` →
   *  "中文 (简体中文）"), which beat guessing from the raw tag. */
  labels?: Record<string, string>;
}

/**
 * Single-select dropdown of the offered language options. Confirm =
 * close + invoke `onConfirm(language)` so the parent can call
 * `install_game(..., { language })`.
 */
export const LanguageSelectModal: FC<Props> = ({
  gameTitle,
  languages,
  onConfirm,
  closeModal,
  preferredTag = i18n.language,
  labels,
}) => {
  const { t } = useTranslation();
  const safeLanguages = languages.length > 0 ? languages : ["en-US"];
  const [selected, setSelected] = useState<string>(() =>
    pickDefaultLanguage(safeLanguages, preferredTag),
  );

  const options: DropdownOption[] = safeLanguages.map((lang) => ({
    data: lang,
    label: labels?.[lang] ?? LANGUAGE_NAMES[lang] ?? lang,
  }));

  return (
    <ConfirmModal
      strTitle={t("installLanguageModal.title")}
      strDescription={t("installLanguageModal.description", {
        title: gameTitle,
      })}
      strOKButtonText={t("installLanguageModal.install")}
      strCancelButtonText={t("common.cancel")}
      onOK={() => {
        onConfirm(selected);
        closeModal?.();
      }}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      <div
        style={{
          padding: 12,
          background: "rgba(0, 0, 0, 0.2)",
          borderRadius: 8,
        }}
      >
        <label
          style={{
            display: "block",
            marginBottom: 8,
            color: "#fff",
            fontSize: 14,
          }}
        >
          {t("installLanguageModal.label")}
        </label>
        <Dropdown
          rgOptions={options}
          selectedOption={selected}
          onChange={(opt: DropdownOption) => setSelected(opt.data as string)}
        />
      </div>
    </ConfirmModal>
  );
};
