import type { LinkProps } from "@tanstack/react-router";
import {
  Inbox,
  ListChecks,
  type LucideIcon,
  Newspaper,
  Search,
  Settings,
  Shield,
} from "lucide-react";

export type NavKey = "inbox" | "tasks" | "digest" | "search" | "settings" | "admin";

export interface NavItem {
  key: NavKey;
  to: NonNullable<LinkProps["to"]>;
  icon: LucideIcon;
  /** Shortcut to open the page (see `docs/DESIGN.md`, keyboard first). */
  shortcut: string;
  /** Shown in the mobile bottom bar; the others live in the "More" sheet. */
  primary: boolean;
  adminOnly?: boolean;
}

export const navItems: readonly NavItem[] = [
  { key: "inbox", to: "/inbox", icon: Inbox, shortcut: "g i", primary: true },
  { key: "tasks", to: "/tasks", icon: ListChecks, shortcut: "g t", primary: true },
  { key: "digest", to: "/digest", icon: Newspaper, shortcut: "g d", primary: true },
  { key: "search", to: "/search", icon: Search, shortcut: "/", primary: true },
  { key: "settings", to: "/settings", icon: Settings, shortcut: "g s", primary: false },
  { key: "admin", to: "/admin", icon: Shield, shortcut: "g a", primary: false, adminOnly: true },
];

export function visibleNavItems(isAdmin: boolean) {
  return navItems.filter((item) => !item.adminOnly || isAdmin);
}
