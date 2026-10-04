import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { aiProvidersQueryOptions, aiSettingsQueryOptions } from "@/api/ai";
import { AssignmentsSection } from "@/components/admin-ai/assignments-section";
import { CloudSection } from "@/components/admin-ai/cloud-section";
import { PerformanceSection } from "@/components/admin-ai/performance-section";
import { ProvidersSection } from "@/components/admin-ai/providers-section";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { retryFailed } from "@/lib/retry-failed";

export const Route = createFileRoute("/admin_/ai")({
  component: AISettingsPage,
});

function AISettingsPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const [adding, setAdding] = useState(false);
  if (!isAdmin) return <Forbidden />;

  return (
    <>
      <PageHeader
        title={t("pages.ai.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/admin" aria-label={t("pages.ai.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
        actions={
          <Button size="sm" onClick={() => setAdding(true)}>
            <Plus aria-hidden />
            {t("pages.ai.providers.add")}
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <AISettingsContent adding={adding} onAddingChange={setAdding} />
      </div>
    </>
  );
}

function AISettingsContent({
  adding,
  onAddingChange,
}: {
  adding: boolean;
  onAddingChange: (open: boolean) => void;
}) {
  const settings = useQuery(aiSettingsQueryOptions);
  const providers = useQuery(aiProvidersQueryOptions);

  if (settings.isPending || providers.isPending) return <ListSkeleton />;
  if (!settings.data || !providers.data) {
    return (
      <InlineError
        error={settings.error ?? providers.error}
        {...retryFailed(settings, providers)}
        className="px-4 py-4 md:px-5"
      />
    );
  }
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-8 px-4 py-6 md:px-6 md:py-8">
      <ProvidersSection
        providers={providers.data}
        adding={adding}
        onAddingChange={onAddingChange}
      />
      <AssignmentsSection settings={settings.data} providers={providers.data} />
      <PerformanceSection settings={settings.data} />
      <CloudSection settings={settings.data} />
    </div>
  );
}
