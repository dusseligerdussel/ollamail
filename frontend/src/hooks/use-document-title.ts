import { useEffect } from "react";
import { useTranslation } from "react-i18next";

/**
 * Names the browser tab after the page (WCAG 2.4.2), e.g. "Tasks – ollamail". Never pass mail
 * content such as a subject: titles end up in the browser history and its sync (docs/PRIVACY.md).
 */
export function useDocumentTitle(title: string | undefined) {
  const { t } = useTranslation();
  const app = t("app.name");
  useEffect(() => {
    if (!title) return;
    document.title = title.includes(app) ? title : `${title} – ${app}`;
  }, [title, app]);
}
