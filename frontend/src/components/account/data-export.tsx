import { useQuery } from "@tanstack/react-query";
import { Download, FileArchive, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  accountPrivacyQueryOptions,
  type DataExport,
  exportDownloadUrl,
  exportsQueryOptions,
  useDeleteExport,
  useRequestExport,
} from "@/api/privacy";
import { InlineError } from "@/components/inline-error";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";

function useFormatters() {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const date = (value: string) => {
    try {
      return new Intl.DateTimeFormat(i18n.resolvedLanguage, {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: timezone,
      }).format(new Date(value));
    } catch {
      return new Date(value).toLocaleString(i18n.resolvedLanguage);
    }
  };
  const size = (bytes: number) => {
    const units = ["byte", "kilobyte", "megabyte", "gigabyte"] as const;
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return new Intl.NumberFormat(i18n.resolvedLanguage, {
      style: "unit",
      unit: units[unit],
      unitDisplay: "short",
      maximumFractionDigits: unit === 0 ? 0 : 1,
    }).format(value);
  };
  return { date, size };
}

function ExportRow({ item }: { item: DataExport }) {
  const { t } = useTranslation();
  const format = useFormatters();
  const remove = useDeleteExport();
  const title = t("privacy.export.item", { date: format.date(item.created_at) });

  let status: string;
  if (item.status === "ready") {
    status = t("privacy.export.ready", {
      size: format.size(item.size ?? 0),
      date: item.expires_at ? format.date(item.expires_at) : "",
    });
  } else if (item.status === "failed") {
    status = t("privacy.export.failed");
  } else {
    status = t("privacy.export.running");
  }

  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <FileArchive className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-ui font-medium">{title}</div>
        <div
          className={
            item.status === "failed" ? "text-ui text-destructive" : "text-ui text-muted-foreground"
          }
        >
          {status}
        </div>
      </div>
      {item.status === "ready" && (
        <Button asChild variant="outline" size="sm" className="text-ui">
          <a href={exportDownloadUrl(item.id)} download>
            <Download aria-hidden="true" />
            {/* Icon only on narrow screens, so date and expiry stay readable. */}
            <span className="sr-only sm:not-sr-only">{t("privacy.export.download")}</span>
          </a>
        </Button>
      )}
      {item.status !== "pending" && item.status !== "running" && (
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={remove.isPending}
          onClick={() => remove.mutate(item.id)}
          aria-label={t("privacy.export.deleteLabel", { name: title })}
        >
          <Trash2 aria-hidden="true" />
        </Button>
      )}
    </li>
  );
}

/** Export of the own data (Art. 15/20): request, progress and expiring download. */
export function DataExportSection() {
  const { t } = useTranslation();
  const options = useQuery(accountPrivacyQueryOptions);
  const exports = useQuery(exportsQueryOptions);
  const request = useRequestExport();
  const inProgress = exports.data?.some(
    (item) => item.status === "pending" || item.status === "running",
  );

  return (
    <>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <div className="text-ui font-medium">{t("privacy.export.title")}</div>
          <div className="text-ui text-muted-foreground">
            {t("privacy.export.description", {
              hours: options.data?.export_expiry_hours ?? 24,
            })}
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0 self-start text-ui sm:self-auto"
          disabled={request.isPending || inProgress || exports.isPending}
          onClick={() => request.mutate()}
        >
          {t("privacy.export.request")}
        </Button>
      </div>
      {exports.isPending && (
        <div role="status" aria-label={t("common.loading")} className="flex gap-3 px-4 py-3">
          <Skeleton className="size-4" />
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-3.5 w-40" />
            <Skeleton className="h-3.5 w-56" />
          </div>
        </div>
      )}
      {exports.isError && <InlineError error={exports.error} className="px-4 py-3.5" />}
      {exports.data && exports.data.length > 0 && (
        <ul className="divide-y" aria-label={t("privacy.export.list")}>
          {exports.data.map((item) => (
            <ExportRow key={item.id} item={item} />
          ))}
        </ul>
      )}
    </>
  );
}
