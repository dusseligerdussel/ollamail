import { Link } from "@tanstack/react-router";
import { Menu } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { LanguageToggleGroup, ThemeToggleGroup } from "@/components/preference-controls";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useCurrentUser } from "@/hooks/use-current-user";

import { visibleNavItems } from "./nav-items";

const tabClass =
  "flex min-w-0 flex-1 flex-col items-center justify-center gap-1 rounded-md text-xs text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring data-[status=active]:text-foreground [&[data-status=active]_svg]:text-brand";

/** Navigation for narrow screens: main pages as tabs, everything else in a sheet. */
export function BottomBar() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const [moreOpen, setMoreOpen] = useState(false);
  const items = visibleNavItems(isAdmin);

  return (
    <nav
      aria-label={t("nav.label")}
      className="shrink-0 border-t bg-sidebar pb-[env(safe-area-inset-bottom)]"
    >
      <ul className="flex h-bottom-bar items-stretch gap-1 px-2 py-1">
        {items
          .filter((item) => item.primary)
          .map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.key} className="flex flex-1">
                <Link to={item.to} className={tabClass}>
                  <Icon className="size-4" aria-hidden="true" />
                  <span className="max-w-full truncate">{t(`nav.${item.key}`)}</span>
                </Link>
              </li>
            );
          })}
        <li className="flex flex-1">
          <button
            type="button"
            className={tabClass}
            aria-haspopup="dialog"
            aria-expanded={moreOpen}
            onClick={() => setMoreOpen(true)}
          >
            <Menu className="size-4" aria-hidden="true" />
            <span>{t("nav.more")}</span>
          </button>
        </li>
      </ul>

      <Sheet open={moreOpen} onOpenChange={setMoreOpen}>
        <SheetContent side="bottom" closeLabel={t("common.close")} className="gap-0 rounded-t-xl">
          <SheetHeader className="px-4 pt-4 pb-2">
            <SheetTitle className="text-sm">{t("nav.more")}</SheetTitle>
            <SheetDescription className="sr-only">{t("nav.moreDescription")}</SheetDescription>
          </SheetHeader>
          <ul className="flex flex-col px-2">
            {items
              .filter((item) => !item.primary)
              .map((item) => {
                const Icon = item.icon;
                return (
                  <li key={item.key}>
                    <Link
                      to={item.to}
                      onClick={() => setMoreOpen(false)}
                      className="flex h-11 items-center gap-3 rounded-md px-2 text-sm outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring data-[status=active]:font-medium [&[data-status=active]_svg]:text-brand"
                    >
                      <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
                      {t(`nav.${item.key}`)}
                    </Link>
                  </li>
                );
              })}
          </ul>
          <div className="mt-2 flex flex-col gap-4 border-t px-4 pt-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))]">
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">{t("theme.label")}</span>
              <ThemeToggleGroup className="w-full" />
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                {t("language.label")}
              </span>
              <LanguageToggleGroup className="w-full" />
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </nav>
  );
}
