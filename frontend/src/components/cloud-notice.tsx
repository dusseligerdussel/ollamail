import { useQuery } from "@tanstack/react-query";
import { Cloud } from "lucide-react";
import { useTranslation } from "react-i18next";

import { aiStatusQueryOptions } from "@/api/ai";

/**
 * Permanent, unobtrusive notice while a cloud provider receives mail content
 * (docs/PRIVACY.md): which provider, for which tasks. Renders nothing otherwise.
 */
export function CloudNotice() {
  const { t } = useTranslation();
  const { data } = useQuery(aiStatusQueryOptions);
  if (!data || data.cloud.length === 0) return null;

  return (
    <aside
      aria-label={t("shell.cloudNotice.label")}
      className="flex shrink-0 items-start gap-2 border-b bg-muted/50 px-4 py-1.5 text-xs text-muted-foreground md:px-5"
    >
      <Cloud aria-hidden className="mt-px size-3.5 shrink-0" />
      <p className="min-w-0">
        <span className="font-medium text-foreground">{t("shell.cloudNotice.label")}</span>
        {data.cloud.map((usage) => (
          <span key={usage.provider}>
            {" · "}
            {t("shell.cloudNotice.text", {
              provider: usage.display_name,
              tasks: usage.tasks.map((task) => t(`pages.ai.tasks.${task}`)).join(", "),
            })}
          </span>
        ))}
      </p>
    </aside>
  );
}
