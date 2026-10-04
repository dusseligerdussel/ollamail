import { CircleAlert, RotateCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { describeApiError } from "@/api/errors";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface InlineErrorProps {
  error: unknown;
  /** Offers to load again, e.g. `query.refetch` of the query that failed. */
  onRetry?: () => unknown;
  /** While the retry is running, e.g. `query.isFetching`. */
  retrying?: boolean;
  className?: string;
}

/** An API error shown in place of the content that failed to load. */
export function InlineError({ error, onRetry, retrying = false, className }: InlineErrorProps) {
  const { t } = useTranslation();
  const { title, description } = describeApiError(error, t);
  return (
    <div role="alert" className={cn("flex items-start gap-2 text-ui", className)}>
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="flex min-w-0 flex-col items-start">
        <p className="text-destructive">{title}</p>
        {description && <p className="text-muted-foreground">{description}</p>}
        {onRetry && (
          <Button
            size="xs"
            variant="outline"
            className="mt-2"
            disabled={retrying}
            onClick={() => void onRetry()}
          >
            <RotateCw aria-hidden />
            {t("common.retry")}
          </Button>
        )}
      </div>
    </div>
  );
}
