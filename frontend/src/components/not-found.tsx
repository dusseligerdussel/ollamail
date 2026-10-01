import { Link } from "@tanstack/react-router";
import { FileQuestion } from "lucide-react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

export function NotFound() {
  const { t } = useTranslation();
  return (
    <EmptyState
      icon={FileQuestion}
      headingLevel={1}
      title={t("notFound.title")}
      description={t("notFound.description")}
      action={
        <Button asChild size="sm">
          <Link to="/inbox">{t("notFound.action")}</Link>
        </Button>
      }
    />
  );
}
