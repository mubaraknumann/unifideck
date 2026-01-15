import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import fr from "./locales/fr.json";

export const SUPPORTED_LOCALES = ["en", "fr"] as const;
export type Locale = typeof SUPPORTED_LOCALES[number];

export const DEFAULT_LOCALE: Locale = "en";

const detectBrowserLocale = (): Locale => {
  const navLang = navigator.language?.split("-")[0];
  return SUPPORTED_LOCALES.includes(navLang as Locale)
    ? (navLang as Locale)
    : DEFAULT_LOCALE;
};

export const loadTranslations = () => {
  i18n
    .use(initReactI18next)
    .init({
      lng: detectBrowserLocale(),
      fallbackLng: DEFAULT_LOCALE,
      resources: {
        en: { translation: en },
        fr: { translation: fr },
      },
      interpolation: { escapeValue: false },
      debug: true,
    });

  console.log("[Unifideck] i18n initialized");
};

export const t = i18n.t.bind(i18n);
