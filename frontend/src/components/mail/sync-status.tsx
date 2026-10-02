import { useTranslation } from "react-i18next";

import type { MailboxSyncStatus } from "@/api/mail";
import { cn } from "@/lib/utils";

/** Translated text for an error code of the sync or the connection test. */
export function useMailErrorText() {
  const { t, i18n } = useTranslation();
  return (code: string | null | undefined) => {
    const key = `mailboxes.errors.${code ?? "unknown"}`;
    return i18n.exists(key) ? t(key as "mailboxes.errors.unknown") : t("mailboxes.errors.unknown");
  };
}

function relative(value: string, locale: string, now = Date.now()) {
  const seconds = Math.round((new Date(value).getTime() - now) / 1000);
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  for (const [unit, size] of steps) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return format.format(0, "second");
}

/** One line: a status dot and what the sync is doing. Updated live through server events. */
export function SyncStatus({
  status,
  className,
}: {
  status: MailboxSyncStatus;
  className?: string;
}) {
  const { t, i18n } = useTranslation();
  const errorText = useMailErrorText();
  const count = new Intl.NumberFormat(i18n.language).format(status.message_count);
  let text: string;
  switch (status.phase) {
    case "paused":
      text = t("mailboxes.status.paused");
      break;
    case "error":
      text = t("mailboxes.status.error", { error: errorText(status.last_error) });
      break;
    case "pending":
      text = t("mailboxes.status.pending");
      break;
    case "importing":
      text = t("mailboxes.status.importing", {
        done: status.folders_imported,
        total: status.folders_total,
        messages: count,
      });
      break;
    case "syncing":
      text = t("mailboxes.status.syncing", { messages: count });
      break;
    default:
      text = status.last_synced_at
        ? t("mailboxes.status.idle", {
            messages: count,
            when: relative(status.last_synced_at, i18n.language),
          })
        : t("mailboxes.status.idleNever", { messages: count });
  }
  const busy = ["pending", "importing", "syncing"].includes(status.phase);
  return (
    <span
      // Live region: screen readers announce sync progress and errors as they arrive.
      role="status"
      // `text-ui` and the colour stay in separate class lists: `cn` would drop one of them.
      className={cn("flex min-w-0 items-center gap-2 text-ui", className)}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-2 shrink-0 rounded-full",
          status.phase === "error" && "bg-destructive",
          busy && "bg-brand motion-safe:animate-pulse",
          (status.phase === "idle" || status.phase === "paused") && "bg-muted-foreground/40",
        )}
      />
      <span
        className={cn(
          "min-w-0 truncate",
          status.phase === "error" ? "text-destructive" : "text-muted-foreground",
        )}
      >
        {text}
      </span>
      {status.folders_failed > 0 && status.phase !== "error" && (
        <span className="shrink-0 text-destructive">
          {t("mailboxes.status.foldersFailed", { count: status.folders_failed })}
        </span>
      )}
    </span>
  );
}
