import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import de from "./locales/de.json";
import en from "./locales/en.json";

export const supportedLanguages = ["de", "en"] as const;
export type SupportedLanguage = (typeof supportedLanguages)[number];

export const defaultNS = "translation";
export const resources = {
  de: { translation: de },
  en: { translation: en },
} as const;

// Key under which an explicit user choice is stored; takes precedence over the browser language.
export const languageStorageKey = "ollamail.language";

i18n.on("languageChanged", (lng) => {
  document.documentElement.lang = lng;
});

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    defaultNS,
    supportedLngs: supportedLanguages,
    nonExplicitSupportedLngs: true,
    load: "languageOnly",
    fallbackLng: "en",
    interpolation: { escapeValue: false },
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: languageStorageKey,
      caches: ["localStorage"],
    },
  });

export default i18n;
