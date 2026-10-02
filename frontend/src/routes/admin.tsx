import { createFileRoute, Link } from "@tanstack/react-router";
import { Archive, ChevronRight, Cpu, ScrollText } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Forbidden } from "@/components/forbidden";
import { PageHeader } from "@/components/page-header";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin")({
  component: AdminPage,
});

function AdminPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  // The API enforces permissions; this only avoids showing an empty admin area to other users.
  if (!isAdmin) return <Forbidden />;

  return (
    <>
      <PageHeader title={t("nav.admin")} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          <section aria-labelledby="admin-security">
            <h2 id="admin-security" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.admin.security")}
            </h2>
            <div className="divide-y rounded-lg border">
              <Link
                to="/admin/audit"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
              >
                <ScrollText aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-ui font-medium">{t("pages.audit.title")}</span>
                  <span className="block text-ui text-muted-foreground">
                    {t("pages.admin.auditDescription")}
                  </span>
                </span>
                <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              </Link>
              <Link
                to="/admin/retention"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
              >
                <Archive aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-ui font-medium">{t("pages.retention.title")}</span>
                  <span className="block text-ui text-muted-foreground">
                    {t("pages.admin.retentionDescription")}
                  </span>
                </span>
                <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              </Link>
            </div>
          </section>
          <section aria-labelledby="admin-ai" className="mt-6">
            <h2 id="admin-ai" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.ai.title")}
            </h2>
            <div className="divide-y rounded-lg border">
              <Link
                to="/admin/ai"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
              >
                <Cpu aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-ui font-medium">{t("pages.admin.ai")}</span>
                  <span className="block text-ui text-muted-foreground">
                    {t("pages.admin.aiDescription")}
                  </span>
                </span>
                <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              </Link>
            </div>
          </section>
          <p className="mt-6 text-ui text-muted-foreground">{t("pages.admin.more")}</p>
        </div>
      </div>
    </>
  );
}
