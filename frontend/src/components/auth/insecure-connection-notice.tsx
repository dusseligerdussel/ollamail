import { Trans, useTranslation } from "react-i18next";

import { Notice } from "@/components/admin/notice";

/** Operations guide: HTTPS or – for tests only – cookies without `Secure`. */
export const INSECURE_CONNECTION_DOCS_URL =
  "https://github.com/dusseligerdussel/ollamail/blob/main/docs/OPERATIONS.md#26-http-ohne-tls-testbetrieb";

/**
 * Explains why setup and sign-in fail on plain `http://` (#142): browsers drop the `Secure`
 * session and CSRF cookies there (except on localhost), so every POST is rejected.
 * Shown proactively outside a secure context and as an error once the server reported a
 * CSRF failure (`failed`). Renders nothing otherwise.
 */
export function InsecureConnectionNotice({ failed = false }: { failed?: boolean }) {
  const { t } = useTranslation();
  if (!failed && window.isSecureContext) return null;
  return (
    <Notice tone={failed ? "error" : "warning"}>
      <div className="flex flex-col gap-1.5">
        <p className="font-medium">
          {failed ? t("auth.insecure.failedTitle") : t("auth.insecure.title")}
        </p>
        <p className="text-muted-foreground">
          {failed ? t("auth.insecure.failedDescription") : t("auth.insecure.description")}
        </p>
        <p className="text-muted-foreground">
          <Trans
            i18nKey="auth.insecure.fix"
            components={{ code: <code className="font-mono text-xs break-all" /> }}
          />
        </p>
        <a
          href={INSECURE_CONNECTION_DOCS_URL}
          target="_blank"
          rel="noreferrer"
          className="self-start underline underline-offset-4 hover:text-foreground"
        >
          {t("auth.insecure.docs")}
        </a>
      </div>
    </Notice>
  );
}
