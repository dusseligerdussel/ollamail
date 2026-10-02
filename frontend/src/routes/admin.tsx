import { createFileRoute, Link } from "@tanstack/react-router";
import {
  Archive,
  ChevronRight,
  Cpu,
  KeyRound,
  type LucideIcon,
  Mails,
  RefreshCw,
  ScrollText,
  Tags,
  UserCog,
  Users,
} from "lucide-react";
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
          <section aria-labelledby="admin-access" className="mb-6">
            <h2 id="admin-access" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.admin.access")}
            </h2>
            <div className="divide-y rounded-lg border">
              <AdminLink
                to="/admin/sign-in"
                icon={KeyRound}
                title={t("pages.signIn.title")}
                description={t("pages.admin.signInDescription")}
              />
              <AdminLink
                to="/admin/role-mapping"
                icon={UserCog}
                title={t("pages.roleMapping.title")}
                description={t("pages.admin.roleMappingDescription")}
              />
              <AdminLink
                to="/admin/users"
                icon={Users}
                title={t("pages.users.title")}
                description={t("pages.admin.usersDescription")}
              />
              <AdminLink
                to="/admin/scim"
                icon={RefreshCw}
                title={t("pages.scim.title")}
                description={t("pages.admin.scimDescription")}
              />
            </div>
          </section>
          <section aria-labelledby="admin-mail" className="mb-6">
            <h2 id="admin-mail" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.admin.mail")}
            </h2>
            <div className="divide-y rounded-lg border">
              <AdminLink
                to="/admin/shared-mailboxes"
                icon={Mails}
                title={t("pages.sharedMailboxes.title")}
                description={t("pages.admin.sharedMailboxesDescription")}
              />
            </div>
          </section>
          <section aria-labelledby="admin-security">
            <h2 id="admin-security" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.admin.security")}
            </h2>
            <div className="divide-y rounded-lg border">
              <Link
                to="/admin/audit"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80"
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
              <AdminLink
                to="/admin/retention"
                icon={Archive}
                title={t("pages.retention.title")}
                description={t("pages.admin.retentionDescription")}
              />
            </div>
          </section>
          <section aria-labelledby="admin-ai" className="mt-6">
            <h2 id="admin-ai" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.ai.title")}
            </h2>
            <div className="divide-y rounded-lg border">
              <Link
                to="/admin/ai"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80"
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
          <section aria-labelledby="admin-categories" className="mt-6">
            <h2 id="admin-categories" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("triage.settings.title")}
            </h2>
            <div className="divide-y rounded-lg border">
              <Link
                to="/admin/categories"
                className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80"
              >
                <Tags aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-ui font-medium">{t("triage.admin.title")}</span>
                  <span className="block text-ui text-muted-foreground">
                    {t("triage.admin.linkDescription")}
                  </span>
                </span>
                <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              </Link>
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

function AdminLink({
  to,
  icon: Icon,
  title,
  description,
}: {
  to:
    | "/admin/sign-in"
    | "/admin/role-mapping"
    | "/admin/users"
    | "/admin/scim"
    | "/admin/retention"
    | "/admin/shared-mailboxes";
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <Link
      to={to}
      className="flex items-center gap-3 px-4 py-3.5 outline-none first:rounded-t-lg last:rounded-b-lg hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80"
    >
      <Icon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block text-ui font-medium">{title}</span>
        <span className="block text-ui text-muted-foreground">{description}</span>
      </span>
      <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
    </Link>
  );
}
