import { createFileRoute, Link } from "@tanstack/react-router";
import { Shield } from "lucide-react";
import { useTranslation } from "react-i18next";

import { EmptyState } from "@/components/empty-state";
import { NotFound } from "@/components/not-found";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin")({
  component: AdminPage,
});

function AdminPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  // The API enforces permissions; this only avoids showing an empty admin area to other users.
  if (!isAdmin) return <NotFound />;

  return (
    <>
      <PageHeader title={t("nav.admin")} />
      <EmptyState
        icon={Shield}
        title={t("pages.admin.emptyTitle")}
        description={t("pages.admin.emptyDescription")}
        action={
          <Button asChild size="sm" variant="outline">
            <Link to="/settings">{t("pages.admin.emptyAction")}</Link>
          </Button>
        }
      />
    </>
  );
}
