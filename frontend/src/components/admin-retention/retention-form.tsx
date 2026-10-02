import { TriangleAlert } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type RetentionField,
  type RetentionSettings,
  type RetentionUpdate,
  retentionFields,
  useUpdateRetention,
} from "@/api/privacy";
import { AdminSection } from "@/components/admin-ai/section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCurrentUser } from "@/hooks/use-current-user";

type Draft = Record<RetentionField, string>;

function toDraft(settings: RetentionSettings): Draft {
  return Object.fromEntries(
    retentionFields.map((field) => [field, String(settings.values[field])]),
  ) as Draft;
}

/** Smallest allowed value: digests are always deleted eventually. */
function minimum(field: RetentionField) {
  return field === "digest_days" ? 1 : 0;
}

function parse(field: RetentionField, value: string): number | undefined {
  const parsed = Number(value);
  if (value.trim() === "" || !Number.isInteger(parsed)) return undefined;
  const max = field === "digest_days" ? 3650 : 36500;
  return parsed >= minimum(field) && parsed <= max ? parsed : undefined;
}

function RetentionRow({
  field,
  settings,
  value,
  onChange,
  onReset,
}: {
  field: RetentionField;
  settings: RetentionSettings;
  value: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const valid = parse(field, value) !== undefined;
  const fallback = settings.defaults[field];
  const overridden = settings.overridden.includes(field);
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={id} className="text-ui font-medium">
            {t(`pages.retention.fields.${field}.label`)}
          </label>
          {overridden && (
            <Badge variant="secondary" className="font-normal">
              {t("pages.retention.custom")}
            </Badge>
          )}
        </div>
        <p id={`${id}-description`} className="text-ui text-muted-foreground">
          {t(`pages.retention.fields.${field}.description`)}
        </p>
        <p className="text-ui text-muted-foreground">
          {fallback === 0
            ? t("pages.retention.defaultForever")
            : t("pages.retention.defaultDays", { count: fallback })}
          {overridden && (
            <>
              {" · "}
              <button
                type="button"
                className="underline underline-offset-2 outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
                onClick={onReset}
              >
                {t("pages.retention.reset")}
              </button>
            </>
          )}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Input
          id={id}
          type="number"
          inputMode="numeric"
          min={minimum(field)}
          className="h-8 w-full sm:w-24"
          aria-invalid={!valid || undefined}
          aria-describedby={`${id}-description ${id}-unit`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
        <span id={`${id}-unit`} className="w-24 text-ui text-muted-foreground">
          {value.trim() === "0" ? t("pages.retention.forever") : t("pages.retention.days")}
        </span>
      </div>
    </div>
  );
}

function LastRun({ settings }: { settings: RetentionSettings }) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const run = settings.last_run;
  if (!run) {
    return <p className="text-ui text-muted-foreground">{t("pages.retention.neverRun")}</p>;
  }
  let date: string;
  try {
    date = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: timezone,
    }).format(new Date(run.finished_at));
  } catch {
    date = new Date(run.finished_at).toLocaleString(i18n.resolvedLanguage);
  }
  return (
    <p className="text-ui text-muted-foreground">
      {t("pages.retention.lastRun", {
        date,
        mails: run.mails,
        attachments: run.attachments,
        chunks: run.search_chunks,
        events: run.audit_events,
      })}
    </p>
  );
}

/** Retention periods per data category; saving sends only the changed fields. */
export function RetentionForm({ settings }: { settings: RetentionSettings }) {
  const { t } = useTranslation();
  const id = useId();
  const update = useUpdateRetention();
  const [draft, setDraft] = useState<Draft>(() => toDraft(settings));
  useEffect(() => setDraft(toDraft(settings)), [settings]);

  const changes: RetentionUpdate = {};
  let valid = true;
  for (const field of retentionFields) {
    const parsed = parse(field, draft[field]);
    if (parsed === undefined) valid = false;
    else if (parsed !== settings.values[field]) changes[field] = parsed;
  }
  const dirty = Object.keys(changes).length > 0;
  const mailDays = parse("mail_days", draft.mail_days) ?? 0;
  const shortMailRetention = mailDays > 0 && mailDays < settings.initial_sync_days;

  const save = (body: RetentionUpdate) =>
    update.mutate(body, { onSuccess: () => toast.success(t("pages.retention.saved")) });

  return (
    <div className="flex flex-col gap-8">
      <AdminSection id={`${id}-periods`} title={t("pages.retention.section")}>
        {retentionFields.map((field) => (
          <RetentionRow
            key={field}
            field={field}
            settings={settings}
            value={draft[field]}
            onChange={(value) => setDraft((current) => ({ ...current, [field]: value }))}
            onReset={() => save({ [field]: null })}
          />
        ))}
        {shortMailRetention && (
          <div role="status" className="flex items-start gap-2 px-4 py-3.5 text-ui">
            <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-foreground" />
            <p className="text-muted-foreground">
              {t("pages.retention.shortMailWarning", { days: settings.initial_sync_days })}
            </p>
          </div>
        )}
        <div className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-ui text-muted-foreground">{t("pages.retention.hint")}</p>
          <Button
            size="sm"
            className="shrink-0 self-end sm:self-auto"
            disabled={!dirty || !valid || update.isPending}
            onClick={() => save(changes)}
          >
            {t("pages.retention.save")}
          </Button>
        </div>
      </AdminSection>
      <AdminSection id={`${id}-job`} title={t("pages.retention.job")}>
        <div className="px-4 py-3.5">
          <LastRun settings={settings} />
        </div>
      </AdminSection>
    </div>
  );
}
