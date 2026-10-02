import { Link } from "@tanstack/react-router";
import { CircleUser, Command, Keyboard, LogOut, Settings } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useCommandPalette } from "@/components/command-palette/command-provider";
import { KeyHint } from "@/components/key-hint";
import { themeIcons } from "@/components/preference-controls";
import { useTheme } from "@/components/theme-provider";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useCurrentUser, useLogout } from "@/hooks/use-current-user";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import { isTheme, themes } from "@/lib/theme";

import { AppMark } from "./app-mark";
import { type NavItem, visibleNavItems } from "./nav-items";
import { SharedMailboxNav } from "./shared-mailbox-nav";
import { useShortcutsOverlay } from "./shortcuts-overlay";

const rowClass =
  "flex h-8 w-full items-center gap-2.5 rounded-md px-2 text-ui text-muted-foreground outline-none transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-sidebar-ring";

const linkClass = `${rowClass} data-[status=active]:bg-sidebar-accent data-[status=active]:font-medium data-[status=active]:text-sidebar-accent-foreground [&[data-status=active]_svg]:text-brand`;

function SidebarLink({ item }: { item: NavItem }) {
  const { t } = useTranslation();
  const Icon = item.icon;
  return (
    <Link to={item.to} className={linkClass}>
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

function UserMenu() {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const logout = useLogout();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={rowClass} aria-label={t("account.menu")}>
        <CircleUser className="size-4 shrink-0" aria-hidden="true" />
        <span className="truncate">{user.display_name}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-56">
        <DropdownMenuLabel className="flex flex-col gap-0.5 font-normal">
          <span className="truncate text-ui font-medium">{user.display_name}</span>
          <span className="truncate text-xs text-muted-foreground">{user.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild className="text-ui">
          <Link to="/settings">
            <Settings aria-hidden="true" />
            {t("account.settings")}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-ui"
          disabled={logout.isPending}
          onSelect={() => logout.mutate()}
        >
          <LogOut aria-hidden="true" />
          {t("auth.logout")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Sidebar() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const palette = useCommandPalette();
  const shortcuts = useShortcutsOverlay();
  // Touch tablets get the sidebar too, but no keyboard hints.
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
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
          {hasKeyboard && <KeyHint keys="mod+k" className="ml-auto" />}
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
        <SharedMailboxNav
          linkClassName={linkClass}
          headingClassName="mt-4 mb-1 px-2 text-xs font-medium text-muted-foreground"
        />
        <ul className="mt-auto flex flex-col gap-px pt-4">
          {secondary.map((item) => (
            <li key={item.key}>
              <SidebarLink item={item} />
            </li>
          ))}
        </ul>
      </nav>

      <div className="flex flex-col gap-px border-t border-sidebar-border p-2">
        <UserMenu />
        <ThemeMenu />
        {hasKeyboard && (
          <button type="button" onClick={() => shortcuts.setOpen(true)} className={rowClass}>
            <Keyboard className="size-4 shrink-0" aria-hidden="true" />
            <span className="truncate">{t("shortcuts.title")}</span>
            <KeyHint keys="?" className="ml-auto" />
          </button>
        )}
      </div>
    </div>
  );
}
