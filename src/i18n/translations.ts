import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enUS from "./locales/en-US.json";
import frFR from "./locales/fr-FR.json";
import ptBR from "./locales/pt-BR.json";
import ruRU from "./locales/ru-RU.json";
import jaJP from "./locales/ja-JP.json";
import deDE from "./locales/de-DE.json";
import esES from "./locales/es-ES.json";
import itIT from "./locales/it-IT.json";
import zhCN from "./locales/zh-CN.json";
import koKR from "./locales/ko-KR.json";
import nlNL from "./locales/nl-NL.json";
import plPL from "./locales/pl-PL.json";
import trTR from "./locales/tr-TR.json";
import ukUA from "./locales/uk-UA.json";

const resources: Record<string, { translation: object }> = {
  "en-US": { translation: enUS },
  "fr-FR": { translation: frFR },
  "pt-BR": { translation: ptBR },
  "ru-RU": { translation: ruRU },
  "ja-JP": { translation: jaJP },
  "de-DE": { translation: deDE },
  "es-ES": { translation: esES },
  "it-IT": { translation: itIT },
  "zh-CN": { translation: zhCN },
  "ko-KR": { translation: koKR },
  "nl-NL": { translation: nlNL },
  "pl-PL": { translation: plPL },
  "tr-TR": { translation: trTR },
  "uk-UA": { translation: ukUA },
};

// Native language names for display in dropdown
export const LANGUAGE_NAMES: Record<string, string> = {
  "en-US": "English",
  "de-DE": "Deutsch",
  "es-ES": "Español",
  "fr-FR": "Français",
  "it-IT": "Italiano",
  "ja-JP": "日本語",
  "ko-KR": "한국어",
  "nl-NL": "Nederlands",
  "pl-PL": "Polski",
  "pt-BR": "Português",
  "ru-RU": "Русский",
  "tr-TR": "Türkçe",
  "zh-CN": "简体中文",
  "uk-UA": "Українська",
};

export const loadTranslations = (savedLanguage?: string) => {
  // Use saved language if provided, otherwise use browser language
  const initialLanguage = savedLanguage && savedLanguage !== "auto"
    ? savedLanguage
    : navigator.language;

  console.log("[Unifideck] i18n browser language:", navigator.language);
  console.log("[Unifideck] i18n using language:", initialLanguage);

  i18n
    .use(initReactI18next)
    .init({
      resources,
      lng: initialLanguage,
      fallbackLng: {
        pt: ["pt-BR"],
        fr: ["fr-FR"],
        en: ["en-US"],
        ru: ["ru-RU"],
        ja: ["ja-JP"],
        de: ["de-DE"],
        es: ["es-ES"],
        it: ["it-IT"],
        zh: ["zh-CN"],
        ko: ["ko-KR"],
        nl: ["nl-NL"],
        pl: ["pl-PL"],
        tr: ["tr-TR"],
        uk: ["uk-UA"],
        default: ["en-US"],
      },
      load: "languageOnly",
      interpolation: { escapeValue: false },
      debug: true,
    });

  console.log("[Unifideck] i18n initialized");
};

export const t = i18n.t.bind(i18n);

// Change language at runtime
export const changeLanguage = async (langCode: string): Promise<void> => {
  console.log("[Unifideck] Changing language to:", langCode);
  await i18n.changeLanguage(langCode);
};

// Get list of supported language codes
export const getSupportedLanguages = (): string[] => {
  return Object.keys(resources);
};

// Get current language
export const getCurrentLanguage = (): string => {
  return i18n.language || "en-US";
};

