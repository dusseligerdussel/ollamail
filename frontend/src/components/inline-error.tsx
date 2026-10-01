import { CircleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { describeApiError } from "@/api/errors";
import { cn } from "@/lib/utils";

/** An API error shown in place of the content that failed to load. */
export function InlineError({ error, className }: { error: unknown; className?: string }) {
  const { t } = useTranslation();
  const { title, description } = describeApiError(error, t);
  return (
    <div role="alert" className={cn("flex items-start gap-2 text-ui", className)}>
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0">
        <p className="text-destructive">{title}</p>
        {description && <p className="text-muted-foreground">{description}</p>}
      </div>
    </div>
  );
}
