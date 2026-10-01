import type { LucideIcon } from "lucide-react";

export const commandGroups = ["navigation", "actions", "appearance", "language", "help"] as const;
export type CommandGroup = (typeof commandGroups)[number];

/** An action in the command palette. Pages register their own via `useCommands`. */
export interface Command {
  /** Unique id; registering the same id again replaces the previous command. */
  id: string;
  /** Translated label. */
  label: string;
  group: CommandGroup;
  icon?: LucideIcon;
  /** Shortcut shown next to the label (display only, see `useShortcut`). */
  shortcut?: string;
  /** Additional search terms. */
  keywords?: string[];
  /** Marks the currently active option, e.g. the selected theme. */
  active?: boolean;
  run: () => void;
}

export function groupCommands(commands: readonly Command[]) {
  return commandGroups
    .map((group) => ({ group, commands: commands.filter((command) => command.group === group) }))
    .filter(({ commands: items }) => items.length > 0);
}
