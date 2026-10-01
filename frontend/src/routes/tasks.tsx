import { createFileRoute, Link } from "@tanstack/react-router";
import { ListChecks } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PlaceholderListPage } from "@/components/placeholder-list-page";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/tasks")({
  component: TasksPage,
});

function TasksPage() {
  const { t } = useTranslation();
  return (
    <PlaceholderListPage
      id="tasks"
      title={t("nav.tasks")}
      icon={ListChecks}
      emptyTitle={t("pages.tasks.emptyTitle")}
      emptyDescription={t("pages.tasks.emptyDescription")}
      action={
        <Button asChild size="sm" variant="outline">
          <Link to="/inbox">{t("pages.tasks.emptyAction")}</Link>
        </Button>
      }
      noSelection={t("pages.tasks.noSelection")}
    />
  );
}
