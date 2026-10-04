import "i18next";
import type { defaultNS, Translation } from "./index";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: typeof defaultNS;
    resources: { translation: Translation };
  }
}
