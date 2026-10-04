import i18n, { type BackendModule, type ResourceKey } from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import type en from "./locales/en.json";

export const supportedLanguages = ["de", "en"] as const;
export type SupportedLanguage = (typeof supportedLanguages)[number];

export const defaultNS = "translation";
/** Shape of the translations (English is the reference, `i18n.test.ts` checks the others). */
export type Translation = typeof en;

/**
 * Each language is a chunk of its own (#188): only the active one is downloaded at start,
 * the other when the user switches to it.
 */
export const loadTranslation: Record<SupportedLanguage, () => Promise<Translation>> = {
  de: () => import("./locales/de.json").then((module) => module.default),
  en: () => import("./locales/en.json").then((module) => module.default),
};

function isSupported(language: string): language is SupportedLanguage {
  return (supportedLanguages as readonly string[]).includes(language);
}

const lazyTranslations: BackendModule = {
  type: "backend",
  init() {},
  read(language, namespace, callback) {
    if (namespace !== defaultNS || !isSupported(language)) {
      callback(null, {});
      return;
    }
    loadTranslation[language]().then(
      (translation) => callback(null, translation as unknown as ResourceKey),
      (error: unknown) => callback(error instanceof Error ? error : String(error), null),
    );
  },
};

// Key under which an explicit user choice is stored; takes precedence over the browser language.
export const languageStorageKey = "ollamail.language";

i18n.on("languageChanged", (lng) => {
  document.documentElement.lang = lng;
});

/** Resolves once the translations of the detected language are loaded. */
export const i18nReady = i18n
  .use(lazyTranslations)
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    defaultNS,
    ns: [defaultNS],
    supportedLngs: supportedLanguages,
    nonExplicitSupportedLngs: true,
    load: "languageOnly",
    // Every language has every key (`i18n.test.ts`): German needs no English fallback, so
    // German users do not download it. Unsupported languages fall back to English.
    fallbackLng: { de: [], default: ["en"] },
    interpolation: { escapeValue: false },
    // Rendering waits for `i18nReady` (main.tsx); a language switch keeps showing the
    // current texts until the new ones are loaded instead of suspending the app.
    react: { useSuspense: false },
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: languageStorageKey,
      caches: ["localStorage"],
    },
  });

export default i18n;
