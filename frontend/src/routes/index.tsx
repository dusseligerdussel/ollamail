import { createFileRoute } from "@tanstack/react-router";
import { Languages } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  component: PlaceholderPage,
});

function PlaceholderPage() {
  const { t, i18n } = useTranslation();
  const nextLanguage = i18n.resolvedLanguage === "de" ? "en" : "de";

  return (
    <main className="mx-auto flex min-h-svh max-w-xl flex-col justify-center gap-4 px-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t("placeholder.title")}</h1>
      <p className="text-muted-foreground">{t("placeholder.description")}</p>
      <div>
        <Button variant="outline" size="sm" onClick={() => void i18n.changeLanguage(nextLanguage)}>
          <Languages strokeWidth={1.5} aria-hidden="true" />
          {t("placeholder.switchLanguage")}
        </Button>
      </div>
    </main>
  );
}
