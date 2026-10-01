import { useNavigate } from "@tanstack/react-router";
import { Keyboard, Languages } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { useCommandPalette, useCommands } from "@/components/command-palette/command-provider";
import { themeIcons } from "@/components/preference-controls";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { useTheme } from "@/components/theme-provider";
import { useCurrentUser } from "@/hooks/use-current-user";
import { supportedLanguages } from "@/i18n";
import type { Command } from "@/lib/commands";
import { themes } from "@/lib/theme";

import { type NavItem, visibleNavItems } from "./nav-items";
import { useShortcutsOverlay } from "./shortcuts-overlay";

function NavShortcut({ item }: { item: NavItem }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  useShortcut(
    {
      id: `nav.${item.key}`,
      keys: item.shortcut,
      group: "navigation",
      description: t("commands.goTo", { page: t(`nav.${item.key}`) }),
    },
    () => void navigate({ to: item.to }),
  );
  return null;
}

/** App-wide shortcuts and command palette entries. */
export function GlobalKeyboard() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { isAdmin } = useCurrentUser();
  const { theme, setTheme } = useTheme();
  const palette = useCommandPalette();
  const overlay = useShortcutsOverlay();
  const items = useMemo(() => visibleNavItems(isAdmin), [isAdmin]);

  useShortcut(
    {
      id: "app.commandPalette",
      keys: "mod+k",
      group: "general",
      description: t("commandPalette.open"),
      allowInInput: true,
    },
    palette.toggle,
  );
  useShortcut(
    { id: "app.shortcuts", keys: "?", group: "general", description: t("shortcuts.show") },
    () => overlay.setOpen(!overlay.open),
  );

  const language = i18n.resolvedLanguage;
  const commands = useMemo<Command[]>(
    () => [
      ...items.map((item) => ({
        id: `nav.${item.key}`,
        label: t("commands.goTo", { page: t(`nav.${item.key}`) }),
        group: "navigation" as const,
        icon: item.icon,
        shortcut: item.shortcut,
        run: () => void navigate({ to: item.to }),
      })),
      ...themes.map((option) => ({
        id: `theme.${option}`,
        label: t(`commands.theme.${option}`),
        group: "appearance" as const,
        icon: themeIcons[option],
        keywords: [t("theme.label")],
        active: theme === option,
        run: () => setTheme(option),
      })),
      ...supportedLanguages.map((option) => ({
        id: `language.${option}`,
        label: t(`language.${option}`),
        group: "language" as const,
        icon: Languages,
        keywords: [t("language.label")],
        active: language === option,
        run: () => void i18n.changeLanguage(option),
      })),
      {
        id: "help.shortcuts",
        label: t("shortcuts.show"),
        group: "help",
        icon: Keyboard,
        shortcut: "?",
        run: () => overlay.setOpen(true),
      },
    ],
    [items, t, i18n, language, navigate, theme, setTheme, overlay.setOpen],
  );
  useCommands(commands);

  return items.map((item) => <NavShortcut key={item.key} item={item} />);
}
