import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ChevronRight, Mails, Plus } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { type SharedMailbox, sharedMailboxesQueryOptions } from "@/api/shared-mailboxes";
import { AdminSubPage } from "@/components/admin/admin-page";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { SyncStatus } from "@/components/mail/sync-status";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import type { Command } from "@/lib/commands";

export const Route = createFileRoute("/admin_/shared-mailboxes")({
  component: SharedMailboxesPage,
});

function SharedMailboxesPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const navigate = useNavigate();
  const commands = useMemo<Command[]>(
    () => [
      {
        id: "sharedMailboxes.add",
        label: t("pages.sharedMailboxes.add"),
        group: "actions",
        icon: Plus,
        run: () => void navigate({ to: "/admin/shared-mailboxes/new" }),
      },
    ],
    [t, navigate],
  );
  useCommands(isAdmin ? commands : []);
  if (!isAdmin) return <Forbidden />;
  return (
    <AdminSubPage
      title={t("pages.sharedMailboxes.title")}
      backLabel={t("pages.sharedMailboxes.back")}
      actions={
        <Button asChild size="sm">
          <Link to="/admin/shared-mailboxes/new">
            <Plus />
            <span className="max-sm:sr-only">{t("pages.sharedMailboxes.add")}</span>
          </Link>
        </Button>
      }
    >
      <SharedMailboxList />
    </AdminSubPage>
  );
}

function SharedMailboxList() {
  const { t } = useTranslation();
  const mailboxes = useQuery(sharedMailboxesQueryOptions);
  if (mailboxes.isPending) {
    return (
      <div className="rounded-lg border">
        <ListSkeleton rows={3} />
      </div>
    );
  }
  if (mailboxes.isError) return <InlineError error={mailboxes.error} />;
  return (
    <>
      {mailboxes.data.length === 0 ? (
        <EmptyState
          icon={Mails}
          title={t("pages.sharedMailboxes.emptyTitle")}
          description={t("pages.sharedMailboxes.emptyDescription")}
          action={
            <Button asChild size="sm">
              <Link to="/admin/shared-mailboxes/new">{t("pages.sharedMailboxes.add")}</Link>
            </Button>
          }
        />
      ) : (
        <ul aria-label={t("pages.sharedMailboxes.title")} className="divide-y rounded-lg border">
          {mailboxes.data.map((mailbox) => (
            <SharedMailboxRow key={mailbox.id} mailbox={mailbox} />
          ))}
        </ul>
      )}
      <p className="mt-4 text-ui text-muted-foreground">{t("pages.sharedMailboxes.privacy")}</p>
    </>
  );
}

function SharedMailboxRow({ mailbox }: { mailbox: SharedMailbox }) {
  const { t } = useTranslation();
  return (
    <li>
      <Link
        to="/admin/shared-mailboxes/$mailboxId"
        params={{ mailboxId: mailbox.id }}
        className="flex items-center gap-3 px-4 py-3 outline-none first:rounded-t-lg last:rounded-b-lg hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-ui font-medium">{mailbox.display_name}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {t(`mailboxes.types.${mailbox.type}`)}
            </span>
          </div>
          {mailbox.display_name !== mailbox.address && (
            <div className="truncate text-ui text-muted-foreground">{mailbox.address}</div>
          )}
          <SyncStatus status={mailbox.status} className="mt-0.5" />
          <div className="mt-0.5 text-xs text-muted-foreground">
            {mailbox.assignments.length === 0
              ? t("pages.sharedMailboxes.notAssigned")
              : t("pages.sharedMailboxes.readers", { count: mailbox.reader_count })}
          </div>
        </div>
        <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      </Link>
    </li>
  );
}
