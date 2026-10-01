import { createFileRoute, Link } from "@tanstack/react-router";
import { Inbox } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PlaceholderListPage } from "@/components/placeholder-list-page";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/inbox")({
  component: InboxPage,
});

function InboxPage() {
  const { t } = useTranslation();
  return (
    <PlaceholderListPage
      id="inbox"
      title={t("nav.inbox")}
      icon={Inbox}
      emptyTitle={t("pages.inbox.emptyTitle")}
      emptyDescription={t("pages.inbox.emptyDescription")}
      action={
        <Button asChild size="sm">
          <Link to="/settings">{t("pages.inbox.emptyAction")}</Link>
        </Button>
      }
      noSelection={t("pages.inbox.noSelection")}
    />
  );
}
