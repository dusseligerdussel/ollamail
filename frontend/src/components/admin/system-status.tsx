import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Check, Circle, Download, RotateCw } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  isPullActive,
  type MailboxProcessing,
  type ModelStatus,
  modelStatusQueryOptions,
  systemOverviewQueryOptions,
  usePullModel,
  useRetryFailed,
} from "@/api/system";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { useMailErrorText } from "@/components/mail/sync-status";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function SectionTitle({ id, children }: { id: string; children: ReactNode }) {
  return (
    <h2 id={id} className="mb-2 text-xs font-medium text-muted-foreground">
      {children}
    </h2>
  );
}

/** Getting-started checklist: models, a mailbox, the digest and the public URL. */
export function GettingStarted() {
  const { t } = useTranslation();
  const models = useQuery(modelStatusQueryOptions);
  const overview = useQuery(systemOverviewQueryOptions);

  if (overview.isError) return null;
  const loading = overview.isPending || models.isPending;
  const modelsReady = !!models.data?.every((model) => model.state === "installed");
  const items: { key: string; done: boolean; title: string; hint: ReactNode }[] = overview.data
    ? [
        {
          key: "models",
          done: modelsReady,
          title: t("pages.admin.start.models"),
          hint: modelsReady ? (
            t("pages.admin.start.modelsDone")
          ) : (
            <a href="#admin-models" className={inlineLink}>
              {t("pages.admin.start.modelsHint")}
            </a>
          ),
        },
        {
          key: "mailbox",
          done: overview.data.mailbox_count > 0,
          title: t("pages.admin.start.mailbox"),
          hint:
            overview.data.mailbox_count > 0 ? (
              t("pages.admin.start.mailboxDone", { count: overview.data.mailbox_count })
            ) : (
              <Link to="/settings/mailboxes/new" className={inlineLink}>
                {t("pages.admin.start.mailboxHint")}
              </Link>
            ),
        },
        {
          key: "digest",
          done: overview.data.digest_enabled && overview.data.digest_scheduler_enabled,
          title: t("pages.admin.start.digest"),
          hint: !overview.data.digest_scheduler_enabled ? (
            t("pages.admin.start.digestSchedulerOff")
          ) : overview.data.digest_enabled ? (
            t("pages.admin.start.digestDone")
          ) : (
            <Link to="/digest/settings" className={inlineLink}>
              {t("pages.admin.start.digestHint")}
            </Link>
          ),
        },
        {
          key: "publicUrl",
          done: overview.data.public_url_set,
          title: t("pages.admin.start.publicUrl"),
          hint: overview.data.public_url_set ? (
            t("pages.admin.start.publicUrlDone")
          ) : (
            <>
              {t("pages.admin.start.publicUrlHint")} <code>OLLAMAIL_AUTH_PUBLIC_URL</code>
            </>
          ),
        },
      ]
    : [];

  return (
    <section aria-labelledby="admin-start" className="mb-6">
      <SectionTitle id="admin-start">{t("pages.admin.start.title")}</SectionTitle>
      {loading ? (
        <div className="rounded-lg border">
          <ListSkeleton rows={4} />
        </div>
      ) : (
        <ul className="divide-y rounded-lg border">
          {items.map((item) => (
            <li key={item.key} className="flex items-start gap-3 px-4 py-3">
              {item.done ? (
                <Check aria-hidden className="mt-0.5 size-4 shrink-0 text-foreground" />
              ) : (
                <Circle aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block text-ui font-medium">
                  {item.title}
                  <span className="sr-only">
                    {" "}
                    ({item.done ? t("pages.admin.start.done") : t("pages.admin.start.open")})
                  </span>
                </span>
                <span className="block text-ui text-muted-foreground">{item.hint}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

const inlineLink =
  "rounded-sm text-foreground underline underline-offset-2 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80";

/** Model per task with its state; missing Ollama models can be downloaded. */
export function ModelStatusSection() {
  const { t } = useTranslation();
  const models = useQuery({ ...modelStatusQueryOptions, meta: { errorToast: false } });

  return (
    <section aria-labelledby="admin-models" className="mb-6">
      <SectionTitle id="admin-models">{t("pages.admin.models.title")}</SectionTitle>
      {models.isPending && (
        <div className="rounded-lg border">
          <ListSkeleton rows={3} />
        </div>
      )}
      {models.isError && <InlineError error={models.error} />}
      {models.data && (
        <ul className="divide-y rounded-lg border">
          {models.data.map((model) => (
            <ModelRow key={model.task} model={model} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ModelRow({ model }: { model: ModelStatus }) {
  const { t, i18n } = useTranslation();
  const pull = usePullModel();
  const active = isPullActive(model.pull);
  const failed = model.pull?.status === "failed" && model.state === "missing";
  const percent = new Intl.NumberFormat(i18n.language, { style: "percent" });
  const size = new Intl.NumberFormat(i18n.language, {
    style: "unit",
    unit: "gigabyte",
    maximumFractionDigits: 1,
  });

  let progress: string | undefined;
  if (active && model.pull) {
    progress = model.pull.total
      ? t("pages.admin.models.downloading", {
          percent: percent.format(model.pull.completed / model.pull.total),
          size: size.format(model.pull.total / 1e9),
        })
      : t("pages.admin.models.queued");
  }

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
      <span className="min-w-0 flex-1">
        <span className="block text-ui font-medium">{t(`pages.ai.tasks.${model.task}`)}</span>
        <span className="block truncate text-ui text-muted-foreground">
          {model.model} · {model.endpoint}
        </span>
        {failed && (
          <span className="block text-ui text-destructive">
            {t("pages.admin.models.pullFailed", {
              reason: t(
                `pages.admin.models.pullErrors.${model.pull?.error_code ?? "pull_failed"}`,
                {
                  defaultValue: t("pages.admin.models.pullErrors.pull_failed"),
                },
              ),
            })}
          </span>
        )}
      </span>
      <span
        className={cn(
          "text-ui",
          model.state === "installed" ? "text-muted-foreground" : "font-medium text-destructive",
        )}
        aria-live="polite"
      >
        {progress ?? t(`pages.admin.models.state.${model.state}`)}
      </span>
      {model.can_pull && !active && (
        <Button
          size="sm"
          variant="outline"
          disabled={pull.isPending}
          onClick={() =>
            pull.mutate(
              { endpoint: model.endpoint, model: model.model },
              { onSuccess: () => toast.success(t("pages.admin.models.pullStarted")) },
            )
          }
        >
          <Download />
          {t("pages.admin.models.pull")}
        </Button>
      )}
    </li>
  );
}

/** Sync state and pending/failed processing per mailbox; counts only, never content. */
export function ProcessingSection() {
  const { t } = useTranslation();
  const overview = useQuery(systemOverviewQueryOptions);

  return (
    <section aria-labelledby="admin-processing" className="mb-6">
      <SectionTitle id="admin-processing">{t("pages.admin.processing.title")}</SectionTitle>
      {overview.isPending && (
        <div className="rounded-lg border">
          <ListSkeleton rows={2} />
        </div>
      )}
      {overview.isError && <InlineError error={overview.error} />}
      {overview.data?.mailboxes.length === 0 && (
        <p className="rounded-lg border px-4 py-3 text-ui text-muted-foreground">
          {t("pages.admin.processing.empty")}
        </p>
      )}
      {overview.data && overview.data.mailboxes.length > 0 && (
        <>
          <ul className="divide-y rounded-lg border">
            {overview.data.mailboxes.map((mailbox) => (
              <ProcessingRow key={mailbox.id} mailbox={mailbox} />
            ))}
          </ul>
          <p className="mt-2 text-ui text-muted-foreground">{t("pages.admin.processing.note")}</p>
        </>
      )}
    </section>
  );
}

function ProcessingRow({ mailbox }: { mailbox: MailboxProcessing }) {
  const { t } = useTranslation();
  const errorText = useMailErrorText();
  const retry = useRetryFailed();
  // Personal mailboxes are named after their owner (their own name may be the address).
  const title = mailbox.is_shared
    ? mailbox.display_name
    : (mailbox.owner_name ?? t("pages.admin.processing.unknownOwner"));
  const kind = mailbox.is_shared
    ? t("pages.admin.processing.shared", { type: t(`mailboxes.types.${mailbox.type}`) })
    : t(`mailboxes.types.${mailbox.type}`);

  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
      <span className="min-w-0 flex-1">
        <span className="block truncate text-ui font-medium">{title}</span>
        <span className="block truncate text-ui text-muted-foreground">
          {kind}
          {" · "}
          {mailbox.sync_phase === "error" ? (
            <span className="text-destructive">
              {t("pages.admin.processing.syncError", { reason: errorText(mailbox.sync_error) })}
            </span>
          ) : (
            t(`pages.admin.processing.sync.${mailbox.sync_phase}`)
          )}
          {!mailbox.processing_enabled && ` · ${t("pages.admin.processing.disabled")}`}
        </span>
      </span>
      <span className="text-ui text-muted-foreground tabular-nums">
        {t("pages.admin.processing.pending", { count: mailbox.pending + mailbox.running })}
      </span>
      <span
        className={cn(
          "text-ui tabular-nums",
          mailbox.failed > 0 ? "font-medium text-destructive" : "text-muted-foreground",
        )}
      >
        {t("pages.admin.processing.failed", { count: mailbox.failed })}
      </span>
      {mailbox.failed > 0 && (
        <Button
          size="sm"
          variant="outline"
          disabled={retry.isPending}
          onClick={() =>
            retry.mutate(mailbox.id, {
              onSuccess: ({ queued }) =>
                toast.success(t("pages.admin.processing.retried", { count: queued })),
            })
          }
        >
          <RotateCw />
          {t("pages.admin.processing.retry")}
        </Button>
      )}
    </li>
  );
}
