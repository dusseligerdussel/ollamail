import { Link } from "@tanstack/react-router";
import { Command, Keyboard } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useCommandPalette } from "@/components/command-palette/command-provider";
import { KeyHint } from "@/components/key-hint";
import { themeIcons } from "@/components/preference-controls";
import { useTheme } from "@/components/theme-provider";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useCurrentUser } from "@/hooks/use-current-user";
import { isTheme, themes } from "@/lib/theme";

import { AppMark } from "./app-mark";
import { type NavItem, visibleNavItems } from "./nav-items";
import { useShortcutsOverlay } from "./shortcuts-overlay";

const rowClass =
  "flex h-8 w-full items-center gap-2.5 rounded-md px-2 text-ui text-muted-foreground outline-none transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring";

function SidebarLink({ item }: { item: NavItem }) {
  const { t } = useTranslation();
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      className={`${rowClass} data-[status=active]:bg-sidebar-accent data-[status=active]:font-medium data-[status=active]:text-sidebar-accent-foreground [&[data-status=active]_svg]:text-brand`}
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span className="truncate">{t(`nav.${item.key}`)}</span>
    </Link>
  );
}

function ThemeMenu() {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();
  const Icon = themeIcons[theme];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={rowClass}>
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        <span className="truncate">{t("theme.label")}</span>
        <span className="ml-auto text-xs">{t(`theme.${theme}`)}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-48">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          {t("theme.label")}
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={theme}
          onValueChange={(value) => isTheme(value) && setTheme(value)}
        >
          {themes.map((option) => {
            const OptionIcon = themeIcons[option];
            return (
              <DropdownMenuRadioItem key={option} value={option} className="text-ui">
                <OptionIcon aria-hidden="true" />
                {t(`theme.${option}`)}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Sidebar() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const palette = useCommandPalette();
  const shortcuts = useShortcutsOverlay();
  const items = visibleNavItems(isAdmin);
  const main = items.filter((item) => item.primary);
  const secondary = items.filter((item) => !item.primary);

  return (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex h-header shrink-0 items-center gap-2 px-4">
        <AppMark className="size-[18px]" />
        <span className="text-sm font-semibold tracking-tight">{t("app.name")}</span>
      </div>

      <div className="px-2 pb-2">
        <button
          type="button"
          onClick={() => palette.setOpen(true)}
          className="flex h-8 w-full items-center gap-2 rounded-md border bg-background px-2 text-ui text-muted-foreground shadow-xs outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        >
          <Command className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">{t("commandPalette.open")}</span>
          <KeyHint keys="mod+k" className="ml-auto" />
        </button>
      </div>

      <nav
        aria-label={t("nav.label")}
        className="flex min-h-0 flex-1 flex-col overflow-y-auto px-2"
      >
        <ul className="flex flex-col gap-px">
          {main.map((item) => (
            <li key={item.key}>
              <SidebarLink item={item} />
            </li>
          ))}
        </ul>
        <ul className="mt-auto flex flex-col gap-px pt-4">
          {secondary.map((item) => (
            <li key={item.key}>
              <SidebarLink item={item} />
            </li>
          ))}
        </ul>
      </nav>

      <div className="flex flex-col gap-px border-t border-sidebar-border p-2">
        <ThemeMenu />
        <button type="button" onClick={() => shortcuts.setOpen(true)} className={rowClass}>
          <Keyboard className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">{t("shortcuts.title")}</span>
          <KeyHint keys="?" className="ml-auto" />
        </button>
      </div>
    </div>
  );
}
