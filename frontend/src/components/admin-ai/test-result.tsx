import { CircleAlert, CircleCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AIConnectionTest } from "@/api/ai";
import { cn } from "@/lib/utils";

/** Outcome of a connection test, announced to screen readers. */
export function TestResult({
  result,
  className,
}: {
  result: AIConnectionTest;
  className?: string;
}) {
  const { t } = useTranslation();
  const message = result.ok
    ? t("pages.ai.test.ok", { count: result.models?.length ?? 0, duration: result.duration_ms })
    : t(`pages.ai.test.${result.error ?? "failed"}`, { status: result.status_code ?? "–" });
  const Icon = result.ok ? CircleCheck : CircleAlert;
  return (
    <p role="status" className={cn("flex items-start gap-2 text-ui", className)}>
      <Icon
        aria-hidden
        className={cn(
          "mt-0.5 size-4 shrink-0",
          result.ok ? "text-muted-foreground" : "text-destructive",
        )}
      />
      <span className={result.ok ? "text-muted-foreground" : "text-destructive"}>{message}</span>
    </p>
  );
}
