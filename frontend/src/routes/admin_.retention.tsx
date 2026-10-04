import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";

import { retentionQueryOptions } from "@/api/privacy";
import { RetentionForm } from "@/components/admin-retention/retention-form";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin_/retention")({
  component: RetentionPage,
});

function RetentionPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  if (!isAdmin) return <Forbidden />;

  return (
    <>
      <PageHeader
        title={t("pages.retention.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/admin" aria-label={t("pages.retention.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <RetentionContent />
      </div>
    </>
  );
}

function RetentionContent() {
  const settings = useQuery(retentionQueryOptions);
  if (settings.isPending) return <ListSkeleton />;
  if (!settings.data) {
    return <InlineError error={settings.error} className="px-4 py-4 md:px-5" />;
  }
  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
      <RetentionForm settings={settings.data} />
    </div>
  );
}
