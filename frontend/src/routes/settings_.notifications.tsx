import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, BellOff } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type NotificationSettings,
  notificationSettingsQueryOptions,
  useUpdateNotificationSettings,
} from "@/api/notifications";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { useCategories, useCategoryName } from "@/components/triage/use-triage";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Switch } from "@/components/ui/switch";
import {
  mailNotificationText,
  type NotificationAccess,
  notificationAccess,
  requestNotificationAccess,
  showNotification,
} from "@/lib/browser-notifications";

export const Route = createFileRoute("/settings_/notifications")({
  component: NotificationsPage,
});

/** Categories offered when notifications are switched on for the first time. */
const DEFAULT_NOTIFICATION_CATEGORIES = ["important", "action_required"];

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

function SwitchRow({
  id,
  label,
  description,
  checked,
  disabled,
  onCheckedChange,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-6 px-4 py-3.5">
      <div className="min-w-0">
        <label htmlFor={id} className="text-ui font-medium">
          {label}
        </label>
        <div id={`${id}-description`} className="text-ui text-muted-foreground">
          {description}
        </div>
      </div>
      <Switch
        id={id}
        className="shrink-0"
        checked={checked}
        disabled={disabled}
        aria-describedby={`${id}-description`}
        onCheckedChange={onCheckedChange}
      />
    </div>
  );
}

function NotificationsPage() {
  const { t } = useTranslation();
  const settings = useQuery(notificationSettingsQueryOptions);

  return (
    <>
      <PageHeader
        title={t("notifications.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings" aria-label={t("notifications.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {settings.isPending && (
            <div
              className="rounded-lg border"
              role="status"
              aria-label={t("notifications.loading")}
            >
              <ListSkeleton rows={4} />
            </div>
          )}
          {settings.isError && <InlineError error={settings.error} />}
          {settings.data &&
            (settings.data.available ? (
              <NotificationsForm settings={settings.data} />
            ) : (
              <EmptyState
                icon={BellOff}
                title={t("notifications.disabledTitle")}
                description={t("notifications.disabledDescription")}
              />
            ))}
        </div>
      </div>
    </>
  );
}

function NotificationsForm({ settings }: { settings: NotificationSettings }) {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const categories = useCategories();
  const update = useUpdateNotificationSettings();
  const [access, setAccess] = useState<NotificationAccess>(notificationAccess);

  const save = (body: Parameters<typeof update.mutate>[0]) =>
    update.mutate(body, { onError: () => toast.error(t("notifications.saveFailed")) });

  const enable = async (checked: boolean) => {
    if (!checked) {
      save({ enabled: false });
      return;
    }
    // Ask the browser first: without its permission nothing could be shown.
    const granted = access === "granted" ? access : await requestNotificationAccess();
    setAccess(granted);
    if (granted !== "granted") return;
    const defaults =
      settings.category_ids.length > 0
        ? undefined
        : (categories.data ?? [])
            .filter((category) =>
              DEFAULT_NOTIFICATION_CATEGORIES.includes(category.builtin_key ?? ""),
            )
            .map((category) => category.id);
    save({ enabled: true, ...(defaults && { category_ids: defaults }) });
  };

  const allowHere = async () => setAccess(await requestNotificationAccess());

  const toggleCategory = (id: string, checked: boolean) =>
    save({
      category_ids: checked
        ? [...settings.category_ids, id]
        : settings.category_ids.filter((other) => other !== id),
    });

  const sendTest = () =>
    showNotification({
      ...mailNotificationText({
        sender: t("notifications.test.sender"),
        category: categoryName({ name: "", builtin_key: "important" }),
        subject: settings.show_subject ? t("notifications.test.subject") : null,
        unknownSender: t("notifications.unknownSender"),
      }),
      tag: "ollamail-test",
      sound: settings.sound,
    });

  const off = !settings.enabled;
  const busy = update.isPending;

  return (
    <>
      <p className="mb-4 text-ui text-muted-foreground">{t("notifications.intro")}</p>
      <Section id="notifications-browser" title={t("notifications.browser")}>
        <SwitchRow
          id="notifications-enabled"
          label={t("notifications.enabled")}
          description={t("notifications.enabledDescription")}
          checked={settings.enabled}
          disabled={busy || access === "unsupported"}
          onCheckedChange={(checked) => void enable(checked)}
        />
        <AccessRow
          access={access}
          enabled={settings.enabled}
          onAllow={() => void allowHere()}
          onTest={sendTest}
        />
      </Section>

      <Section id="notifications-categories" title={t("notifications.categories")} className="mt-8">
        <div className="flex flex-col gap-3 px-4 py-3.5">
          <div className="text-ui text-muted-foreground">
            {t("notifications.categoriesDescription")}
          </div>
          {categories.isPending ? (
            <ListSkeleton rows={3} />
          ) : categories.isError ? (
            <InlineError error={categories.error} />
          ) : (
            <ul className="flex flex-col gap-2.5">
              {categories.visible.map((category) => {
                const id = `notifications-category-${category.id}`;
                return (
                  <li key={category.id} className="flex items-center gap-2.5">
                    <Checkbox
                      id={id}
                      checked={settings.category_ids.includes(category.id)}
                      disabled={off || busy}
                      onCheckedChange={(value) => toggleCategory(category.id, value === true)}
                    />
                    <label htmlFor={id} className="min-w-0 truncate text-ui">
                      {categoryName(category)}
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </Section>

      <Section id="notifications-content" title={t("notifications.content")} className="mt-8">
        <SwitchRow
          id="notifications-subject"
          label={t("notifications.showSubject")}
          description={t("notifications.showSubjectDescription")}
          checked={settings.show_subject}
          disabled={off || busy}
          onCheckedChange={(checked) => save({ show_subject: checked })}
        />
        <SwitchRow
          id="notifications-sound"
          label={t("notifications.sound")}
          description={t("notifications.soundDescription")}
          checked={settings.sound}
          disabled={off || busy}
          onCheckedChange={(checked) => save({ sound: checked })}
        />
      </Section>
      <p className="mt-6 text-ui text-muted-foreground">{t("notifications.privacy")}</p>
    </>
  );
}

function AccessRow({
  access,
  enabled,
  onAllow,
  onTest,
}: {
  access: NotificationAccess;
  enabled: boolean;
  onAllow: () => void;
  onTest: () => void;
}) {
  const { t } = useTranslation();
  const action =
    access === "default" && enabled ? (
      <Button size="sm" variant="outline" className="shrink-0 text-ui" onClick={onAllow}>
        {t("notifications.access.allow")}
      </Button>
    ) : access === "granted" && enabled ? (
      <Button size="sm" variant="outline" className="shrink-0 text-ui" onClick={onTest}>
        {t("notifications.test.send")}
      </Button>
    ) : null;
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <div className="text-ui font-medium">{t("notifications.access.label")}</div>
        <div className="text-ui text-muted-foreground" role="status">
          {t(`notifications.access.${access}`)}
        </div>
      </div>
      {action}
    </div>
  );
}
