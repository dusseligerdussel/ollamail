import { createFileRoute } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { PageHeader } from "@/components/page-header";
import { LanguageToggleGroup, ThemeToggleGroup } from "@/components/preference-controls";

export const Route = createFileRoute("/settings")({
  component: SettingsPage,
});

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
                <ThemeToggleGroup />
              </SettingRow>
              <SettingRow
                label={t("language.label")}
                description={t("pages.settings.languageDescription")}
              >
                <LanguageToggleGroup />
              </SettingRow>
            </div>
          </section>
          <p className="mt-6 text-ui text-muted-foreground">{t("pages.settings.more")}</p>
        </div>
      </div>
    </>
  );
}
