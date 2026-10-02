import { type ErrorComponentProps, useRouter } from "@tanstack/react-router";
import { CircleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { describeApiError } from "@/api/errors";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

/**
 * Shown when the app cannot start, e.g. the server is unreachable while checking the session.
 * Rendered without the app shell, since it is unknown whether anyone is signed in.
 */
export function RootError({ error }: ErrorComponentProps) {
  const { t } = useTranslation();
  const router = useRouter();
  const { title, description } = describeApiError(error, t);
  return (
    <main id="main" className="flex min-h-dvh flex-col bg-background">
      <EmptyState
        icon={CircleAlert}
        headingLevel={1}
        title={t("rootError.title")}
        description={
          <div role="alert">
            <p>{title}</p>
            {description && <p>{description}</p>}
          </div>
        }
        action={
          <Button size="sm" onClick={() => void router.invalidate()}>
            {t("rootError.retry")}
          </Button>
        }
      />
    </main>
  );
}
