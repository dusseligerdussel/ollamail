import { createFileRoute, Link } from "@tanstack/react-router";
import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/search")({
  component: SearchPage,
});

function SearchPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("nav.search")} />
      <EmptyState
        icon={Search}
        title={t("pages.search.emptyTitle")}
        description={t("pages.search.emptyDescription")}
        action={
          <Button asChild size="sm" variant="outline">
            <Link to="/inbox">{t("pages.search.emptyAction")}</Link>
          </Button>
        }
      />
    </>
  );
}
