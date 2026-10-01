import { createFileRoute, Link } from "@tanstack/react-router";
import { Newspaper } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PlaceholderListPage } from "@/components/placeholder-list-page";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/digest")({
  component: DigestPage,
});

function DigestPage() {
  const { t } = useTranslation();
  return (
    <PlaceholderListPage
      id="digest"
      title={t("nav.digest")}
      icon={Newspaper}
      emptyTitle={t("pages.digest.emptyTitle")}
      emptyDescription={t("pages.digest.emptyDescription")}
      action={
        <Button asChild size="sm" variant="outline">
          <Link to="/settings">{t("pages.digest.emptyAction")}</Link>
        </Button>
      }
      noSelection={t("pages.digest.noSelection")}
    />
  );
}
