import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ChevronRight, LogOut, Mail, ShieldCheck, Tags } from "lucide-react";
import { type ReactNode, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { healthQueryOptions } from "@/api/health";
import { DataExportSection } from "@/components/account/data-export";
import { DeleteAccountSection } from "@/components/account/delete-account";
import { SessionsList } from "@/components/account/sessions-list";
import { InlineError } from "@/components/inline-error";
import { PageHeader } from "@/components/page-header";
import { LanguageToggleGroup, ThemeToggleGroup } from "@/components/preference-controls";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useChangeLanguage,
  useCurrentUser,
  useLogout,
  useUpdateProfile,
} from "@/hooks/use-current-user";
import { timeZoneOptions } from "@/lib/time-zones";

export const Route = createFileRoute("/settings")({
  component: SettingsPage,
});

// Full width when stacked on narrow screens (as in the "More" sheet), one shared width otherwise.
const toggleGroupClass = "w-full sm:w-72";

function SettingRow({
  label,
  labelFor,
  description,
  children,
}: {
  label: string;
  /** Id of the control, if it is a single form field. */
  labelFor?: string;
  description: string;
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
        <div className="text-ui text-muted-foreground">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function ServerStatus() {
  const { t } = useTranslation();
  const health = useQuery(healthQueryOptions);
  if (health.isPending) {
    return (
      <Skeleton
        role="status"
        className="h-4 w-24"
        aria-label={t("pages.settings.serverChecking")}
      />
    );
  }
  if (health.isError) {
    return <InlineError error={health.error} />;
  }
  return <span className="text-ui text-muted-foreground">{t("pages.settings.serverOk")}</span>;
}

function SettingsSection({
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

function TimeZoneSelect() {
  const user = useCurrentUser();
  const update = useUpdateProfile();
  const options = useMemo(() => timeZoneOptions(user.timezone), [user.timezone]);
  return (
    <NativeSelect
      id="settings-timezone"
      size="sm"
      className="w-full"
      value={(update.isPending && update.variables.timezone) || user.timezone}
      disabled={update.isPending}
      onChange={(event) => update.mutate({ timezone: event.target.value })}
    >
      {options.map((zone) => (
        <NativeSelectOption key={zone} value={zone}>
          {zone.replaceAll("_", " ")}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

function AccountSection() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const logout = useLogout();
  return (
    <SettingsSection id="settings-account" title={t("account.title")}>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <div className="truncate text-ui font-medium">{user.display_name}</div>
          <div className="truncate text-ui text-muted-foreground">
            {user.email} · {t(`account.roles.${user.role}`)}
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0 self-start text-ui sm:self-auto"
          disabled={logout.isPending}
          onClick={() => logout.mutate()}
        >
          <LogOut aria-hidden="true" />
          {t("auth.logout")}
        </Button>
      </div>
      <SettingRow
        label={t("account.timeZone")}
        labelFor="settings-timezone"
        description={t("account.timeZoneDescription")}
      >
        <div className={toggleGroupClass}>
          <TimeZoneSelect />
        </div>
      </SettingRow>
      <Link
        to="/settings/security"
        className="flex items-center gap-3 rounded-b-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        <ShieldCheck aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="block text-ui font-medium">{t("account.security.title")}</span>
          <span className="block text-ui text-muted-foreground">
            {t("account.security.linkDescription")}
          </span>
        </span>
        <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      </Link>
    </SettingsSection>
  );
}

function SettingsPage() {
  const { t } = useTranslation();
  const changeLanguage = useChangeLanguage();
  return (
    <>
      <PageHeader title={t("nav.settings")} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          <AccountSection />
          <SettingsSection id="settings-mailboxes" title={t("mailboxes.title")} className="mt-8">
            <Link
              to="/settings/mailboxes"
              className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              <Mail aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block text-ui font-medium">{t("mailboxes.manage")}</span>
                <span className="block text-ui text-muted-foreground">
                  {t("mailboxes.settingsDescription")}
                </span>
              </span>
              <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
            </Link>
          </SettingsSection>
          <SettingsSection
            id="settings-categories"
            title={t("triage.settings.title")}
            className="mt-8"
          >
            <Link
              to="/settings/categories"
              className="flex items-center gap-3 rounded-lg px-4 py-3.5 outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              <Tags aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block text-ui font-medium">{t("triage.commands.manage")}</span>
                <span className="block text-ui text-muted-foreground">
                  {t("triage.settings.linkDescription")}
                </span>
              </span>
              <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
            </Link>
          </SettingsSection>
          <SettingsSection
            id="settings-appearance"
            title={t("pages.settings.appearance")}
            className="mt-8"
          >
            <SettingRow label={t("theme.label")} description={t("pages.settings.themeDescription")}>
              <ThemeToggleGroup className={toggleGroupClass} />
            </SettingRow>
            <SettingRow
              label={t("language.label")}
              description={t("pages.settings.languageDescription")}
            >
              <LanguageToggleGroup className={toggleGroupClass} onChange={changeLanguage} />
            </SettingRow>
          </SettingsSection>
          <SettingsSection
            id="settings-sessions"
            title={t("account.sessions.title")}
            className="mt-8"
          >
            <SessionsList />
          </SettingsSection>
          <SettingsSection id="settings-privacy" title={t("privacy.section")} className="mt-8">
            <DataExportSection />
            <DeleteAccountSection />
          </SettingsSection>
          <SettingsSection id="settings-server" title={t("pages.settings.server")} className="mt-8">
            <SettingRow
              label={t("pages.settings.serverConnection")}
              description={t("pages.settings.serverDescription")}
            >
              <ServerStatus />
            </SettingRow>
          </SettingsSection>
          <p className="mt-6 text-ui text-muted-foreground">{t("pages.settings.more")}</p>
        </div>
      </div>
    </>
  );
}
