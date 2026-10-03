import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { mailboxesQueryOptions } from "@/api/mail";
import { modelStatusQueryOptions } from "@/api/system";
import { useMailErrorText } from "@/components/mail/sync-status";
import { useCurrentUser } from "@/hooks/use-current-user";

/**
 * Unobtrusive notices above the content when something needs attention: one of the
 * user's own mailboxes cannot sync (everyone), or a model is missing or the LLM endpoint
 * is unreachable (admins). Renders nothing otherwise.
 */
export function SystemNotices() {
  const { isAdmin } = useCurrentUser();
  return (
    <>
      <MailboxErrorNotice />
      {isAdmin && <ModelNotice />}
    </>
  );
}

function NoticeBar({ label, children }: { label: string; children: ReactNode }) {
  return (
    <aside
      aria-label={label}
      className="flex shrink-0 items-start gap-2 border-b bg-muted/50 px-4 py-1.5 text-xs text-muted-foreground md:px-5"
    >
      <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0 text-destructive" />
      <p className="min-w-0">
        <span className="font-medium text-foreground">{label}</span>
        {" · "}
        {children}
      </p>
    </aside>
  );
}

const linkClass =
  "rounded-sm font-medium text-foreground underline underline-offset-2 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80";

function MailboxErrorNotice() {
  const { t } = useTranslation();
  const errorText = useMailErrorText();
  const pathname = useLocation({ select: (location) => location.pathname });
  const { data } = useQuery(mailboxesQueryOptions);
  const failing = data?.filter((mailbox) => !mailbox.is_shared && mailbox.status.phase === "error");
  // The mailbox settings show the error next to each mailbox.
  if (!failing?.length || pathname.startsWith("/settings/mailboxes")) return null;

  const [first] = failing;
  return (
    <NoticeBar label={t("shell.syncNotice.label")}>
      {first && failing.length === 1
        ? t("shell.syncNotice.one", {
            mailbox: first.display_name,
            reason: errorText(first.status.last_error),
          })
        : t("shell.syncNotice.many", { count: failing.length })}{" "}
      <Link to="/settings/mailboxes" className={linkClass}>
        {t("shell.syncNotice.action")}
      </Link>
    </NoticeBar>
  );
}

function ModelNotice() {
  const { t } = useTranslation();
  const pathname = useLocation({ select: (location) => location.pathname });
  const { data } = useQuery(modelStatusQueryOptions);
  // The admin overview shows the details.
  if (!data || pathname === "/admin") return null;

  const unreachable = [
    ...new Set(data.filter((m) => m.state === "unreachable").map((m) => m.endpoint)),
  ];
  const missing = [...new Set(data.filter((m) => m.state === "missing").map((m) => m.model))];
  if (unreachable.length === 0 && missing.length === 0) return null;

  return (
    <NoticeBar
      label={
        unreachable.length > 0 ? t("shell.modelNotice.unreachable") : t("shell.modelNotice.missing")
      }
    >
      {unreachable.length > 0
        ? t("shell.modelNotice.unreachableText", {
            endpoints: unreachable.join(", "),
            count: unreachable.length,
          })
        : t("shell.modelNotice.missingText", {
            models: missing.join(", "),
            count: missing.length,
          })}{" "}
      <Link to="/admin" className={linkClass}>
        {t("shell.modelNotice.action")}
      </Link>
    </NoticeBar>
  );
}
