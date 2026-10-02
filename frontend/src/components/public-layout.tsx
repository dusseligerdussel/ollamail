import { Outlet } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppMark } from "@/components/app-shell/app-mark";
import { LanguageToggleGroup } from "@/components/preference-controls";
import { Toaster } from "@/components/ui/sonner";

/** Layout for pages without a session (login, setup): a narrow centred column, no navigation. */
export function PublicLayout() {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <header className="flex h-header shrink-0 items-center gap-2 px-4 md:px-6">
        <AppMark className="size-[18px]" />
        <span className="text-sm font-semibold tracking-tight">{t("app.name")}</span>
      </header>
      <main
        id="main"
        tabIndex={-1}
        className="flex flex-1 flex-col items-center px-4 pt-8 pb-12 outline-none sm:justify-center sm:pt-0"
      >
        <div className="w-full max-w-sm">
          <Outlet />
        </div>
      </main>
      <footer className="flex shrink-0 justify-center px-4 pb-6">
        <LanguageToggleGroup />
      </footer>
      <Toaster position="top-center" />
    </div>
  );
}
