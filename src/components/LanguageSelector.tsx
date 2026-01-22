/**
 * Language Selector Component
 *
 * Dropdown to select display language for UI and game downloads.
 * Saves preference to backend and updates i18n at runtime.
 */

import { FC, useState, useEffect } from "react";
import { call } from "@decky/api";
import { PanelSection, Dropdown, DropdownOption } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { LANGUAGE_NAMES, changeLanguage, getSupportedLanguages } from "../i18n";

/**
 * Language Selector Component
 * Shows dropdown with all supported languages in native names.
 */
export const LanguageSelector: FC = () => {
  const { t } = useTranslation();
  const [selectedLanguage, setSelectedLanguage] = useState<string>("auto");
  const [loading, setLoading] = useState(true);

  // Load saved language preference on mount
  useEffect(() => {
    const loadLanguagePreference = async () => {
      try {
        const result = await call<[], { success: boolean; language: string }>(
          "get_language_preference",
        );
        if (result.success && result.language) {
          setSelectedLanguage(result.language);
        } else {
          // Default to current i18n language or auto
          setSelectedLanguage("auto");
        }
      } catch (error) {
        console.error("[LanguageSelector] Error loading preference:", error);
        setSelectedLanguage("auto");
      }
      setLoading(false);
    };
    loadLanguagePreference();
  }, []);

  // Handle language change
  const handleLanguageChange = async (option: DropdownOption) => {
    const langCode = option.data as string;
    setSelectedLanguage(langCode);

    try {
      // Save to backend
      await call<[string], { success: boolean }>(
        "set_language_preference",
        langCode,
      );

      // Update i18n immediately
      if (langCode === "auto") {
        // Use browser language
        await changeLanguage(navigator.language);
      } else {
        await changeLanguage(langCode);
      }

      console.log("[LanguageSelector] Language changed to:", langCode);
    } catch (error) {
      console.error("[LanguageSelector] Error saving preference:", error);
    }
  };

  // Build dropdown options
  const dropdownOptions: DropdownOption[] = [
    {
      data: "auto",
      label: t("languageSettings.autoDetect"),
    },
    ...getSupportedLanguages().map((code) => ({
      data: code,
      label: LANGUAGE_NAMES[code] || code,
    })),
  ];

  const selectedOption = dropdownOptions.find(
    (opt) => opt.data === selectedLanguage,
  );

  if (loading) {
    return null; // Don't render until loaded
  }

  return (
    <PanelSection title={t("languageSettings.title")}>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "4px",
          width: "100%",
        }}
      >
        <label>{t("languageSettings.label")}</label>
        <Dropdown
          rgOptions={dropdownOptions}
          selectedOption={selectedOption?.data}
          onChange={handleLanguageChange}
        />
        <p style={{ fontSize: "0.85em", color: "#666" }}>
          {t("languageSettings.description")}
        </p>
      </div>
    </PanelSection>
  );
};

export default LanguageSelector;
