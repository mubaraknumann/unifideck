/**
 * Per-Game Language Selector Component
 *
 * Allows users to set a language preference for individual games.
 * Shows dropdown with "Auto (Global)" option + supported languages.
 * Only displayed for non-Steam games (Epic/GOG/Amazon).
 */

import { FC, useState, useEffect } from "react";
import { call } from "@decky/api";
import { Dropdown, DropdownOption } from "@decky/ui";
import { useTranslation } from "react-i18next";

interface GameLanguageSelectorProps {
  store: string;
  gameId: string;
}

interface LanguageOption {
  code: string;
  label: string;
}

/**
 * Per-Game Language Selector Component
 */
export const GameLanguageSelector: FC<GameLanguageSelectorProps> = ({
  store,
  gameId,
}) => {
  const { t } = useTranslation();
  const [selectedLanguage, setSelectedLanguage] = useState<string>("auto");
  const [availableLanguages, setAvailableLanguages] = useState<
    LanguageOption[]
  >([]);
  const [loading, setLoading] = useState(true);

  // Load current preference and available languages on mount
  useEffect(() => {
    const loadLanguageData = async () => {
      try {
        // Get available languages for this store/game
        const optionsResult = await call<
          [string, string],
          { success: boolean; languages: LanguageOption[] }
        >("get_game_language_options", store, gameId);

        if (optionsResult.success && optionsResult.languages) {
          setAvailableLanguages(optionsResult.languages);
        }

        // Get current preference
        const prefResult = await call<
          [string, string],
          {
            success: boolean;
            preference: string | null;
            resolved: string;
          }
        >("get_game_language_preference", store, gameId);

        if (prefResult.success) {
          setSelectedLanguage(prefResult.preference || "auto");
        }
      } catch (error) {
        console.error("[GameLanguageSelector] Error loading data:", error);
      }
      setLoading(false);
    };

    loadLanguageData();
  }, [store, gameId]);

  // Handle language change
  const handleLanguageChange = async (option: DropdownOption) => {
    const langCode = option.data as string;
    setSelectedLanguage(langCode);

    try {
      const result = await call<
        [string, string, string | null],
        { success: boolean }
      >(
        "set_game_language_preference",
        store,
        gameId,
        langCode === "auto" ? null : langCode,
      );

      if (result.success) {
        console.log(
          `[GameLanguageSelector] Language preference saved for ${store}:${gameId}:`,
          langCode,
        );
      }
    } catch (error) {
      console.error("[GameLanguageSelector] Error saving preference:", error);
    }
  };

  // Build dropdown options
  const dropdownOptions: DropdownOption[] = [
    {
      data: "auto",
      label: t("gameLanguage.auto"),
    },
    ...availableLanguages.map((lang) => ({
      data: lang.code,
      label: lang.label,
    })),
  ];

  const selectedOption = dropdownOptions.find(
    (opt) => opt.data === selectedLanguage,
  );

  if (loading || availableLanguages.length === 0) {
    return null; // Don't render until loaded or if no languages available
  }

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "6px",
        padding: "12px 0",
        width: "100%",
      }}
    >
      <label style={{ fontSize: "14px", fontWeight: 600 }}>
        {t("gameLanguage.title")}
      </label>
      <Dropdown
        rgOptions={dropdownOptions}
        selectedOption={selectedOption?.data}
        onChange={handleLanguageChange}
      />
      <p style={{ fontSize: "0.85em", color: "#999", marginTop: "4px" }}>
        {t("gameLanguage.note")}
      </p>
    </div>
  );
};

export default GameLanguageSelector;
