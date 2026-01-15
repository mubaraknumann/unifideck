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
};

export const loadTranslations = () => {
  console.log("[Unifideck] i18n browser language:", navigator.language);

  i18n
    .use(initReactI18next)
    .init({
      resources,
      lng: navigator.language,
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
        default: ["en-US"],
      },
      load: "languageOnly",
      interpolation: { escapeValue: false },
      debug: true,
    });

  console.log("[Unifideck] i18n initialized");
};

export const t = i18n.t.bind(i18n);
