import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import {
  ArrowLeft,
  FolderTree,
  Inbox,
  Mail,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  deleteMailbox,
  type Mailbox,
  mailboxesQueryOptions,
  syncMailbox,
  updateMailbox,
} from "@/api/mail";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { FolderSheet } from "@/components/mail/folder-sheet";
import { SyncStatus, useMailErrorText } from "@/components/mail/sync-status";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Command } from "@/lib/commands";

interface MailboxesSearch {
  /** Result of an OAuth connect flow (Gmail: via `/`, Microsoft 365: `graph=…`). */
  connected?: boolean;
  error?: string;
}

const CODE = /^[a-z][a-z0-9_]{0,63}$/;

export const Route = createFileRoute("/settings_/mailboxes")({
  validateSearch: (search: Record<string, unknown>): MailboxesSearch => {
    const connected =
      search.connected === true || search.connected === "1" || search.graph === "connected";
    const rawError = search.graph === "error" ? search.reason : search.error;
    const error = typeof rawError === "string" && CODE.test(rawError) ? rawError : undefined;
    return { ...(connected && { connected }), ...(error && { error }) };
  },
  component: MailboxesPage,
});

function MailboxesPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const search = Route.useSearch();
  const errorText = useMailErrorText();
  const mailboxes = useQuery(mailboxesQueryOptions);
  const [folders, setFolders] = useState<Mailbox>();
  const [removing, setRemoving] = useState<Mailbox>();

  // Report the result of an OAuth connect flow once, then clean the URL.
  useEffect(() => {
    if (!search.connected && !search.error) return;
    if (search.connected) toast.success(t("mailboxes.connected"));
    else toast.error(t("mailboxes.connectFailed"), { description: errorText(search.error) });
    void navigate({ to: "/settings/mailboxes", search: {}, replace: true });
  }, [search.connected, search.error, navigate, t, errorText]);

  const commands = useMemo<Command[]>(
    () => [
      {
        id: "mailboxes.add",
        label: t("mailboxes.add"),
        group: "actions",
        icon: Plus,
        run: () => void navigate({ to: "/settings/mailboxes/new" }),
      },
    ],
    [t, navigate],
  );
  useCommands(commands);

  return (
    <>
      <PageHeader
        title={t("mailboxes.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings" aria-label={t("mailboxes.backToSettings")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
        actions={
          mailboxes.data && mailboxes.data.length > 0 ? (
            <Button asChild size="sm">
              <Link to="/settings/mailboxes/new">
                <Plus />
                {t("mailboxes.add")}
              </Link>
            </Button>
          ) : undefined
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {mailboxes.isPending && (
            <div className="rounded-lg border">
              <ListSkeleton rows={3} />
            </div>
          )}
          {mailboxes.isError && <InlineError error={mailboxes.error} />}
          {mailboxes.data?.length === 0 && (
            <EmptyState
              icon={Inbox}
              title={t("mailboxes.emptyTitle")}
              description={t("mailboxes.emptyDescription")}
              action={
                <Button asChild size="sm">
                  <Link to="/settings/mailboxes/new">{t("mailboxes.add")}</Link>
                </Button>
              }
            />
          )}
          {mailboxes.data && mailboxes.data.length > 0 && (
            <ul aria-label={t("mailboxes.title")} className="divide-y rounded-lg border">
              {mailboxes.data.map((mailbox) => (
                <MailboxRow
                  key={mailbox.id}
                  mailbox={mailbox}
                  onFolders={() => setFolders(mailbox)}
                  onRemove={() => setRemoving(mailbox)}
                />
              ))}
            </ul>
          )}
          {mailboxes.data && mailboxes.data.length > 0 && (
            <p className="mt-4 text-ui text-muted-foreground">{t("mailboxes.privacyNote")}</p>
          )}
        </div>
      </div>
      <FolderSheet mailbox={folders} onOpenChange={(open) => !open && setFolders(undefined)} />
      <RemoveDialog mailbox={removing} onClose={() => setRemoving(undefined)} />
    </>
  );
}

function MailboxRow({
  mailbox,
  onFolders,
  onRemove,
}: {
  mailbox: Mailbox;
  onFolders: () => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["mailbox"] });
  const sync = useMutation({
    mutationFn: () => syncMailbox(mailbox.id),
    onSuccess: () => {
      toast.success(t("mailboxes.syncRequested"));
      void refresh();
    },
  });
  const pause = useMutation({
    mutationFn: (enabled: boolean) => updateMailbox(mailbox.id, { sync_enabled: enabled }),
    onSuccess: () => void refresh(),
  });

  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground">
        <Mail aria-hidden className="size-4" />
      </div>
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
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={t("mailboxes.actions", { name: mailbox.display_name })}
          >
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            disabled={!mailbox.sync_enabled || sync.isPending}
            onSelect={() => sync.mutate()}
          >
            <RefreshCw />
            {t("mailboxes.syncNow")}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={onFolders}>
            <FolderTree />
            {t("mailboxes.folders.open")}
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={pause.isPending}
            onSelect={() => pause.mutate(!mailbox.sync_enabled)}
          >
            {mailbox.sync_enabled ? <Pause /> : <Play />}
            {mailbox.sync_enabled ? t("mailboxes.pause") : t("mailboxes.resume")}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={onRemove}>
            <Trash2 />
            {t("mailboxes.remove")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

function RemoveDialog({ mailbox, onClose }: { mailbox: Mailbox | undefined; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: string) => deleteMailbox(id),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
      void queryClient.invalidateQueries({ queryKey: ["message"] });
      toast.success(
        t("mailboxes.removed", {
          count: result.messages,
          formatted: new Intl.NumberFormat(i18n.language).format(result.messages),
        }),
      );
      onClose();
    },
  });

  return (
    <Dialog open={!!mailbox} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("mailboxes.removeTitle")}</DialogTitle>
          <DialogDescription>
            {t("mailboxes.removeDescription", {
              name: mailbox?.display_name,
              count: mailbox?.status.message_count ?? 0,
              formatted: new Intl.NumberFormat(i18n.language).format(
                mailbox?.status.message_count ?? 0,
              ),
            })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("mailboxes.cancel")}
          </Button>
          <Button
            variant="destructive"
            disabled={!mailbox || remove.isPending}
            onClick={() => mailbox && remove.mutate(mailbox.id)}
          >
            {t("mailboxes.remove")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
