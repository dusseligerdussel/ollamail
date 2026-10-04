import { RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { isApiError } from "@/api/errors";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Seconds left until `error` may be retried (its `Retry-After`), counting down to 0. */
export function useRetryWait(error: unknown): number {
  const seconds = isApiError(error) ? (error.retryAfter ?? 0) : 0;
  const [left, setLeft] = useState(seconds);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new error starts a new wait
  useEffect(() => {
    setLeft(seconds);
    if (seconds <= 0) return;
    const until = Date.now() + seconds * 1000;
    const timer = setInterval(() => {
      const remaining = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      setLeft(remaining);
      if (remaining === 0) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [error, seconds]);
  return left;
}

/** "Try again" below an error; disabled until the server's `Retry-After` has passed. */
export function RetryButton({
  error,
  onRetry,
  retrying = false,
  className,
}: {
  error: unknown;
  onRetry: () => unknown;
  retrying?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const wait = useRetryWait(error);
  return (
    <Button
      size="xs"
      variant="outline"
      className={cn("mt-2", className)}
      disabled={retrying || wait > 0}
      onClick={() => void onRetry()}
    >
      <RotateCw aria-hidden />
      {wait > 0 ? t("common.retryIn", { count: wait }) : t("common.retry")}
    </Button>
  );
}
