import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, ListChecks, RefreshCw, Unplug } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  connectedTaskListsQueryOptions,
  type ExportTarget,
  todoExportQueryOptions,
  useDisconnectTodoExport,
  useSyncTodoExport,
  useUpdateExportList,
  useUpdateExportMode,
} from "@/api/todo-export";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { PageHeader } from "@/components/page-header";
import { ConnectForm, ModeChoice, useExportErrorText } from "@/components/task-export/connect-form";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import { formatDateTime } from "@/lib/mail-format";

interface TaskExportSearch {
  /** Result of the Microsoft To Do sign-in (`mstodo=connected` or `mstodo=error&reason=…`). */
  mstodo?: "connected" | "error";
  reason?: string;
  /** Result of the Google Tasks sign-in (`gtasks=connected` or `gtasks_error=<code>`). */
  gtasks?: "connected";
  gtasks_error?: string;
}

const CODE = /^[a-z][a-z0-9_]{0,63}$/;

export const Route = createFileRoute("/settings_/task-export")({
  validateSearch: (search: Record<string, unknown>): TaskExportSearch => {
    const mstodo =
      search.mstodo === "connected" || search.mstodo === "error" ? search.mstodo : undefined;
    const reason =
      typeof search.reason === "string" && CODE.test(search.reason) ? search.reason : undefined;
    const gtasks = search.gtasks === "connected" ? search.gtasks : undefined;
    const gtasksError =
      typeof search.gtasks_error === "string" && CODE.test(search.gtasks_error)
        ? search.gtasks_error
        : undefined;
    return {
      ...(mstodo && { mstodo }),
      ...(mstodo === "error" && reason && { reason }),
      ...(gtasks && { gtasks }),
      ...(gtasksError && { gtasks_error: gtasksError }),
    };
  },
  component: TaskExportPage,
});

function TaskExportPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const search = Route.useSearch();
  const errorText = useExportErrorText();
  const settings = useQuery(todoExportQueryOptions);
  // Back from the Microsoft sign-in: continue with list and mode.
  const [signedIn] = useState(search.mstodo === "connected");
  const [editing, setEditing] = useState(signedIn);
  // After a failed Google sign-in the form opens on Google Tasks again.
  const [retrySink] = useState(() => (search.gtasks_error ? ("gtasks" as const) : undefined));

  // Report a failed sign-in once, then clean the URL.
  useEffect(() => {
    if (!search.mstodo) return;
    if (search.mstodo === "error") {
      // A fixed ID: effects run twice in development (StrictMode), the toast shows once.
      toast.error(t("taskExport.mstodo.signInFailed"), {
        id: "task-export-mstodo",
        description: errorText(search.reason),
      });
    }
    void navigate({ to: "/settings/task-export", search: {}, replace: true });
  }, [search.mstodo, search.reason, navigate, t, errorText]);

  // Google Tasks: the target is saved by the callback; report the result once.
  useEffect(() => {
    if (!search.gtasks && !search.gtasks_error) return;
    // A fixed ID: effects run twice in development (StrictMode), the toast shows once.
    const id = "task-export-oauth";
    if (search.gtasks) toast.success(t("taskExport.google.connected"), { id });
    else {
      toast.error(t("taskExport.google.failed"), {
        id,
        description: errorText(search.gtasks_error),
      });
    }
    void navigate({ to: "/settings/task-export", search: {}, replace: true });
  }, [search.gtasks, search.gtasks_error, navigate, t, errorText]);

  let content: ReactNode;
  if (settings.isPending) {
    content = (
      <div role="status" aria-label={t("taskExport.loading")} className="flex flex-col gap-3">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  } else if (settings.isError) {
    content = (
      <InlineError
        error={settings.error}
        onRetry={settings.refetch}
        retrying={settings.isFetching}
      />
    );
  } else {
    const { target } = settings.data;
    const sinks = settings.data.available_sinks ?? [];
    if (!target && sinks.length === 0) {
      content = (
        <EmptyState
          icon={ListChecks}
          title={t("taskExport.disabledTitle")}
          description={t("taskExport.disabledDescription")}
          className="rounded-lg border"
        />
      );
    } else if (!target || editing) {
      content = (
        <ConnectForm
          sinks={sinks}
          current={target ?? undefined}
          signedIn={signedIn ? "mstodo" : undefined}
          initialSink={retrySink}
          onDone={() => {
            setEditing(false);
            toast.success(t(target ? "taskExport.saved" : "taskExport.enabled"));
          }}
          onCancel={target ? () => setEditing(false) : undefined}
        />
      );
    } else {
      content = <ConnectedTarget target={target} onEdit={() => setEditing(true)} />;
    }
  }

  return (
    <>
      <PageHeader
        title={t("taskExport.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings" aria-label={t("taskExport.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          <p className="mb-6 text-ui text-muted-foreground">{t("taskExport.intro")}</p>
          {content}
        </div>
      </div>
    </>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 px-4 py-3 sm:flex-row sm:items-baseline sm:justify-between sm:gap-6">
      <dt className="shrink-0 text-ui text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-ui break-words sm:text-right">{children}</dd>
    </div>
  );
}

function SyncStatus({ target }: { target: ExportTarget }) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const errorText = useExportErrorText();
  if (!target.active) {
    return <span className="text-destructive">{t("taskExport.inactive")}</span>;
  }
  if (target.last_error) {
    return <span className="text-destructive">{errorText(target.last_error, target.sink)}</span>;
  }
  if (!target.last_sync_at) {
    return <span className="text-muted-foreground">{t("taskExport.neverSynced")}</span>;
  }
  return (
    <span>
      {t("taskExport.lastSync", {
        time: formatDateTime(target.last_sync_at, i18n.language, timezone),
      })}
    </span>
  );
}

/** Another list of the connected account (OAuth targets connect to the default list). */
function ListChoice({ target }: { target: ExportTarget }) {
  const { t } = useTranslation();
  const lists = useQuery(connectedTaskListsQueryOptions(target.active));
  const update = useUpdateExportList();
  const options = lists.data ?? [{ id: target.list_id, name: target.list_name }];
  const value = (update.isPending && update.variables) || target.list_id;
  return (
    <NativeSelect
      aria-label={t("taskExport.list")}
      className="w-full sm:w-56"
      value={value}
      disabled={!lists.data || update.isPending}
      onChange={(event) =>
        update.mutate(event.target.value, {
          onSuccess: () => toast.success(t("taskExport.listChanged")),
        })
      }
    >
      {options.map((list) => (
        <NativeSelectOption key={list.id} value={list.id}>
          {list.name}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

function ConnectedTarget({ target, onEdit }: { target: ExportTarget; onEdit: () => void }) {
  const { t } = useTranslation();
  const mode = useUpdateExportMode();
  const sync = useSyncTodoExport();
  const disconnect = useDisconnectTodoExport();
  const [confirming, setConfirming] = useState(false);
  const { counts } = target;

  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="task-export-connection">
        <h2 id="task-export-connection" className="mb-2 text-xs font-medium text-muted-foreground">
          {t("taskExport.connection")}
        </h2>
        <dl className="divide-y rounded-lg border">
          <Row label={t("taskExport.target")}>{t(`taskExport.sinks.${target.sink}.title`)}</Row>
          {target.url && <Row label={t("taskExport.url")}>{target.url}</Row>}
          {target.username && (
            <Row
              label={t(
                target.sink === "mstodo" ? "taskExport.mstodo.account" : "taskExport.username",
              )}
            >
              {target.username}
            </Row>
          )}
          <Row label={t("taskExport.list")}>
            {target.sink === "gtasks" ? <ListChoice target={target} /> : target.list_name}
          </Row>
          <Row label={t("taskExport.status")}>
            <SyncStatus target={target} />
          </Row>
          <Row label={t("taskExport.tasks")}>
            {t("taskExport.counts", { count: counts.synced })}
            {counts.pending > 0 && ` · ${t("taskExport.pendingCount", { count: counts.pending })}`}
            {counts.error > 0 && (
              <span className="text-destructive">
                {` · ${t("taskExport.errorCount", { count: counts.error })}`}
              </span>
            )}
          </Row>
        </dl>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={sync.isPending || !target.active}
            onClick={() =>
              sync.mutate(undefined, { onSuccess: () => toast.success(t("taskExport.syncQueued")) })
            }
          >
            <RefreshCw aria-hidden />
            {t("taskExport.syncNow")}
          </Button>
          <Button size="sm" variant="outline" onClick={onEdit}>
            {t("taskExport.change")}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setConfirming(true)}>
            <Unplug aria-hidden />
            {t("taskExport.disconnect")}
          </Button>
        </div>
      </section>
      <section aria-labelledby="task-export-mode">
        <h2 id="task-export-mode" className="mb-2 text-xs font-medium text-muted-foreground">
          {t("taskExport.mode.label")}
        </h2>
        <ModeChoice
          value={(mode.isPending && mode.variables) || target.mode}
          disabled={mode.isPending}
          onChange={(next) => mode.mutate(next)}
        />
      </section>
      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("taskExport.disconnectTitle")}</DialogTitle>
            <DialogDescription>{t("taskExport.disconnectDescription")}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(false)}>
              {t("taskExport.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={disconnect.isPending}
              onClick={() =>
                disconnect.mutate(undefined, {
                  onSuccess: () => {
                    setConfirming(false);
                    toast.success(t("taskExport.disconnected"));
                  },
                })
              }
            >
              {t("taskExport.disconnect")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
