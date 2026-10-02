import { Link } from "@tanstack/react-router";
import { ShieldX } from "lucide-react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

/** 403: the page exists, but the signed-in user may not open it. */
export function Forbidden() {
  const { t } = useTranslation();
  return (
    <EmptyState
      icon={ShieldX}
      headingLevel={1}
      title={t("forbidden.title")}
      description={t("forbidden.description")}
      action={
        <Button asChild size="sm">
          <Link to="/inbox">{t("forbidden.action")}</Link>
        </Button>
      }
    />
  );
}
