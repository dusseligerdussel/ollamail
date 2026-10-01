import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { healthQueryOptions } from "@/api/health";
import { InlineError } from "@/components/inline-error";
import { PageHeader } from "@/components/page-header";
import { LanguageToggleGroup, ThemeToggleGroup } from "@/components/preference-controls";
import { Skeleton } from "@/components/ui/skeleton";

export const Route = createFileRoute("/settings")({
  component: SettingsPage,
});

// Full width when stacked on narrow screens (as in the "More" sheet), one shared width otherwise.
const toggleGroupClass = "w-full sm:w-72";

function SettingRow({
  label,
  description,
  children,
}: {
  label: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <div className="text-ui font-medium">{label}</div>
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

function SettingsPage() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader title={t("nav.settings")} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          <section aria-labelledby="settings-appearance">
            <h2 id="settings-appearance" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.settings.appearance")}
            </h2>
            <div className="divide-y rounded-lg border">
              <SettingRow
                label={t("theme.label")}
                description={t("pages.settings.themeDescription")}
              >
                <ThemeToggleGroup className={toggleGroupClass} />
              </SettingRow>
              <SettingRow
                label={t("language.label")}
                description={t("pages.settings.languageDescription")}
              >
                <LanguageToggleGroup className={toggleGroupClass} />
              </SettingRow>
            </div>
          </section>
          <section aria-labelledby="settings-server" className="mt-8">
            <h2 id="settings-server" className="mb-2 text-xs font-medium text-muted-foreground">
              {t("pages.settings.server")}
            </h2>
            <div className="divide-y rounded-lg border">
              <SettingRow
                label={t("pages.settings.serverConnection")}
                description={t("pages.settings.serverDescription")}
              >
                <ServerStatus />
              </SettingRow>
            </div>
          </section>
          <p className="mt-6 text-ui text-muted-foreground">{t("pages.settings.more")}</p>
        </div>
      </div>
    </>
  );
}
