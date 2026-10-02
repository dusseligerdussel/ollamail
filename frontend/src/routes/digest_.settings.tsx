import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { type ReactNode, useEffect, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type DigestSettings,
  type DigestSettingsUpdate,
  type DigestVoice,
  digestSettingsQueryOptions,
  digestVoicesQueryOptions,
  useCreateDigest,
  useUpdateDigestSettings,
} from "@/api/digest";
import { mailboxesQueryOptions } from "@/api/mail";
import { FeedSection } from "@/components/digest/feed-section";
import { InlineError } from "@/components/inline-error";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useCurrentUser } from "@/hooks/use-current-user";
import { formatDateTime } from "@/lib/mail-format";
import { timeZoneOptions } from "@/lib/time-zones";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/digest_/settings")({
  component: DigestSettingsPage,
});

const controlClass = "w-full sm:w-72";
// Segmented control as in the account settings.
const groupClass = "h-8 rounded-md bg-muted p-0.5";
const itemClass =
  "h-7 flex-1 rounded-[calc(var(--radius)-3px)] px-2 text-ui font-normal text-muted-foreground hover:bg-transparent hover:text-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow-xs dark:data-[state=on]:bg-input/30";

function SettingRow({
  label,
  labelFor,
  description,
  children,
}: {
  label: string;
  labelFor?: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        {labelFor ? (
          <label htmlFor={labelFor} className="text-ui font-medium">
            {label}
          </label>
        ) : (
          <div className="text-ui font-medium">{label}</div>
        )}
        {description && <div className="text-ui text-muted-foreground">{description}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Section({
  id,
  title,
  className,
  children,
}: {
  id: string;
  title: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className={className}>
      <h2 id={id} className="mb-2 text-xs font-medium text-muted-foreground">
        {title}
      </h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  );
}

/** Localized short weekday names, Monday (0) first as in the API. */
function useWeekdays() {
  const { i18n } = useTranslation();
  return useMemo(() => {
    const format = new Intl.DateTimeFormat(i18n.language, { weekday: "short", timeZone: "UTC" });
    const long = new Intl.DateTimeFormat(i18n.language, { weekday: "long", timeZone: "UTC" });
    // 2024-01-01 was a Monday.
    return Array.from({ length: 7 }, (_, day) => {
      const date = new Date(Date.UTC(2024, 0, 1 + day));
      return { day, short: format.format(date), long: long.format(date) };
    });
  }, [i18n.language]);
}

const VOICE = /^[a-z]{2,3}_[A-Z]{2}-([a-z0-9_]+)-(x_low|low|medium|high)$/;

function useVoiceLabel() {
  const { t } = useTranslation();
  return (voice: string) => {
    const match = VOICE.exec(voice);
    if (!match?.[1] || !match[2]) return voice;
    const name = match[1].replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
    return `${name} · ${t(`digest.settings.quality.${match[2] as "x_low" | "low" | "medium" | "high"}`)}`;
  };
}

function DigestSettingsPage() {
  const { t } = useTranslation();
  const settings = useQuery(digestSettingsQueryOptions);
  return (
    <>
      <PageHeader
        title={t("digest.settings.title")}
        leading={
          <Button asChild variant="ghost" size="icon-sm">
            <Link to="/digest" aria-label={t("digest.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {settings.isPending ? (
            <SettingsSkeleton />
          ) : settings.isError ? (
            <InlineError error={settings.error} />
          ) : (
            <SettingsForm settings={settings.data} />
          )}
        </div>
      </div>
    </>
  );
}

function SettingsSkeleton() {
  const { t } = useTranslation();
  return (
    <div role="status" aria-busy="true" className="divide-y rounded-lg border">
      <span className="sr-only">{t("common.loading")}</span>
      {Array.from({ length: 6 }, (_, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: placeholder rows have no identity
        <div key={index} className="flex items-center justify-between gap-6 px-4 py-4">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-3 w-32" />
            <Skeleton className="h-3 w-56" />
          </div>
          <Skeleton className="h-8 w-40" />
        </div>
      ))}
    </div>
  );
}

function SettingsForm({ settings }: { settings: DigestSettings }) {
  const { t, i18n } = useTranslation();
  const user = useCurrentUser();
  const navigate = useNavigate();
  const update = useUpdateDigestSettings();
  const create = useCreateDigest();
  const voices = useQuery(digestVoicesQueryOptions);
  const mailboxes = useQuery(mailboxesQueryOptions);
  const weekdays = useWeekdays();
  const voiceLabel = useVoiceLabel();
  const ids = {
    enabled: useId(),
    time: useId(),
    timezone: useId(),
    language: useId(),
    voice: useId(),
  };

  const save = (body: DigestSettingsUpdate) => update.mutate(body);
  // Optimistic display of the pending change.
  const pending = update.isPending ? update.variables : undefined;
  const current = { ...settings, ...pending } as DigestSettings;

  const [time, setTime] = useState(settings.delivery_time.slice(0, 5));
  useEffect(() => setTime(settings.delivery_time.slice(0, 5)), [settings.delivery_time]);
  const saveTime = () => {
    if (/^\d{2}:\d{2}$/.test(time) && time !== settings.delivery_time.slice(0, 5)) {
      save({ delivery_time: `${time}:00` });
    }
  };

  const zones = useMemo(() => timeZoneOptions(settings.timezone ?? undefined), [settings.timezone]);
  const language = current.language ?? settings.effective_language;
  const languageVoices: DigestVoice[] =
    voices.data?.filter((voice) => voice.language === language) ?? [];
  const defaultVoice = languageVoices.find((voice) => voice.default);
  const languageName = (code: string) => t(`language.${code as "de" | "en"}`);

  const selectedMailboxes = current.mailbox_ids;
  const toggleMailbox = (id: string, checked: boolean) => {
    const all = mailboxes.data?.map((mailbox) => mailbox.id) ?? [];
    const selected = new Set(selectedMailboxes ?? all);
    if (checked) selected.add(id);
    else selected.delete(id);
    // All selected means "all mailboxes", including ones added later.
    save({ mailbox_ids: all.every((mailbox) => selected.has(mailbox)) ? null : [...selected] });
  };

  const next = settings.next_run_at;

  return (
    <>
      <Section id="digest-schedule" title={t("digest.settings.schedule")}>
        <SettingRow
          label={t("digest.settings.enabled")}
          labelFor={ids.enabled}
          description={
            current.enabled && next
              ? t("digest.settings.nextRun", {
                  date: formatDateTime(next, i18n.language, user.timezone),
                })
              : t("digest.settings.enabledDescription")
          }
        >
          <Switch
            id={ids.enabled}
            checked={current.enabled}
            onCheckedChange={(enabled) => save({ enabled })}
          />
        </SettingRow>
        <SettingRow
          label={t("digest.settings.time")}
          labelFor={ids.time}
          description={t("digest.settings.timeDescription")}
        >
          <Input
            id={ids.time}
            type="time"
            step={60}
            value={time}
            onChange={(event) => setTime(event.target.value)}
            onBlur={saveTime}
            onKeyDown={(event) => event.key === "Enter" && saveTime()}
            className={cn(controlClass, "h-8")}
          />
        </SettingRow>
        <SettingRow label={t("digest.settings.weekdays")}>
          <ToggleGroup
            type="multiple"
            spacing={0.5}
            value={current.weekdays.map(String)}
            onValueChange={(value) => {
              if (value.length > 0) save({ weekdays: value.map(Number) });
            }}
            aria-label={t("digest.settings.weekdays")}
            className={cn(groupClass, controlClass)}
          >
            {weekdays.map(({ day, short, long }) => (
              <ToggleGroupItem
                key={day}
                value={String(day)}
                aria-label={long}
                className={itemClass}
              >
                {short}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </SettingRow>
        <SettingRow
          label={t("digest.settings.timezone")}
          labelFor={ids.timezone}
          description={t("digest.settings.timezoneDescription")}
        >
          <NativeSelect
            id={ids.timezone}
            size="sm"
            className={controlClass}
            value={current.timezone ?? ""}
            onChange={(event) => save({ timezone: event.target.value || null })}
          >
            <NativeSelectOption value="">
              {t("digest.settings.fromProfile", { value: user.timezone.replaceAll("_", " ") })}
            </NativeSelectOption>
            {zones.map((zone) => (
              <NativeSelectOption key={zone} value={zone}>
                {zone.replaceAll("_", " ")}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </SettingRow>
      </Section>

      <Section id="digest-content" title={t("digest.settings.content")} className="mt-8">
        <SettingRow
          label={t("digest.settings.length")}
          description={t("digest.settings.lengthDescription")}
        >
          <ToggleGroup
            type="single"
            spacing={0.5}
            value={current.length}
            onValueChange={(value) => {
              if (value === "short" || value === "normal") save({ length: value });
            }}
            aria-label={t("digest.settings.length")}
            className={cn(groupClass, controlClass)}
          >
            <ToggleGroupItem value="short" className={itemClass}>
              {t("digest.settings.lengths.short")}
            </ToggleGroupItem>
            <ToggleGroupItem value="normal" className={itemClass}>
              {t("digest.settings.lengths.normal")}
            </ToggleGroupItem>
          </ToggleGroup>
        </SettingRow>
        <SettingRow
          label={t("digest.settings.language")}
          labelFor={ids.language}
          description={t("digest.settings.languageDescription")}
        >
          <NativeSelect
            id={ids.language}
            size="sm"
            className={controlClass}
            value={current.language ?? ""}
            onChange={(event) => {
              const value = event.target.value;
              // A voice belongs to one language; switch back to that language's default.
              save({
                language: value === "de" || value === "en" ? value : null,
                ...(current.voice && { voice: null }),
              });
            }}
          >
            <NativeSelectOption value="">
              {t("digest.settings.fromProfile", { value: languageName(user.language) })}
            </NativeSelectOption>
            <NativeSelectOption value="de">{languageName("de")}</NativeSelectOption>
            <NativeSelectOption value="en">{languageName("en")}</NativeSelectOption>
          </NativeSelect>
        </SettingRow>
        <SettingRow
          label={t("digest.settings.voice")}
          labelFor={ids.voice}
          description={t("digest.settings.voiceDescription")}
        >
          <NativeSelect
            id={ids.voice}
            size="sm"
            className={controlClass}
            value={current.voice ?? ""}
            disabled={voices.isPending}
            onChange={(event) => save({ voice: event.target.value || null })}
          >
            <NativeSelectOption value="">
              {defaultVoice
                ? t("digest.settings.defaultVoiceNamed", { voice: voiceLabel(defaultVoice.id) })
                : t("digest.settings.defaultVoice")}
            </NativeSelectOption>
            {languageVoices
              .filter((voice) => !voice.default)
              .map((voice) => (
                <NativeSelectOption key={voice.id} value={voice.id}>
                  {voiceLabel(voice.id)}
                </NativeSelectOption>
              ))}
            {current.voice && !languageVoices.some((voice) => voice.id === current.voice) && (
              <NativeSelectOption value={current.voice}>
                {voiceLabel(current.voice)}
              </NativeSelectOption>
            )}
          </NativeSelect>
        </SettingRow>
        <div className="flex flex-col gap-3 px-4 py-3.5">
          <div>
            <div className="text-ui font-medium">{t("digest.settings.mailboxes")}</div>
            <div className="text-ui text-muted-foreground">
              {t("digest.settings.mailboxesDescription")}
            </div>
          </div>
          {mailboxes.isPending ? (
            <Skeleton className="h-4 w-40" />
          ) : mailboxes.isError ? (
            <InlineError error={mailboxes.error} />
          ) : mailboxes.data.length === 0 ? (
            <p className="text-ui text-muted-foreground">
              {t("digest.settings.noMailboxes")}{" "}
              <Link
                to="/settings/mailboxes/new"
                className="text-foreground underline underline-offset-2"
              >
                {t("mailboxes.add")}
              </Link>
            </p>
          ) : (
            <ul className="flex flex-col gap-2.5">
              {mailboxes.data.map((mailbox) => {
                const checked =
                  selectedMailboxes === null || selectedMailboxes.includes(mailbox.id);
                const onlyOne = checked && selectedMailboxes?.length === 1;
                const id = `digest-mailbox-${mailbox.id}`;
                return (
                  <li key={mailbox.id} className="flex items-center gap-2.5">
                    <Checkbox
                      id={id}
                      checked={checked}
                      disabled={onlyOne || (mailboxes.data.length === 1 && checked)}
                      onCheckedChange={(value) => toggleMailbox(mailbox.id, value === true)}
                    />
                    <label htmlFor={id} className="min-w-0 truncate text-ui">
                      {mailbox.display_name}
                      <span className="text-muted-foreground"> · {mailbox.address}</span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Section>

      <Section id="digest-now" title={t("digest.settings.manual")} className="mt-8">
        <SettingRow
          label={t("digest.generateNow")}
          description={t("digest.settings.generateDescription")}
        >
          <Button
            size="sm"
            variant="outline"
            className="text-ui"
            disabled={create.isPending}
            onClick={() =>
              create.mutate(undefined, {
                onSuccess: (digest) =>
                  void navigate({ to: "/digest", search: { digest: digest.id } }),
              })
            }
          >
            {t("digest.generateNow")}
          </Button>
        </SettingRow>
      </Section>

      <Section id="digest-feed" title={t("digest.feed.section")} className="mt-8">
        <FeedSection feed={settings.feed} />
      </Section>
    </>
  );
}
