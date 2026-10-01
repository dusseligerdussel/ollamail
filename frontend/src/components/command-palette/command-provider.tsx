import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import type { Command } from "@/lib/commands";
import { createRegistry, type Registry } from "@/lib/registry";

interface CommandContextValue {
  registry: Registry<Command>;
  open: boolean;
  setOpen: (open: boolean) => void;
}

const CommandContext = createContext<CommandContextValue | null>(null);

export function CommandProvider({ children }: { children: ReactNode }) {
  const [registry] = useState(() => createRegistry<Command>());
  const [open, setOpen] = useState(false);
  const value = useMemo(() => ({ registry, open, setOpen }), [registry, open]);
  return <CommandContext value={value}>{children}</CommandContext>;
}

function useCommandContext() {
  const context = useContext(CommandContext);
  if (!context) throw new Error("Command hooks must be used within <CommandProvider>");
  return context;
}

/**
 * Registers commands while the calling component is mounted. Pass a memoized array; the commands
 * are re-registered whenever its identity changes.
 */
export function useCommands(commands: readonly Command[]) {
  const { registry } = useCommandContext();
  useEffect(() => {
    const unregister = commands.map((command) => registry.register(command));
    return () => {
      for (const fn of unregister) fn();
    };
  }, [registry, commands]);
}

export function useRegisteredCommands(): readonly Command[] {
  const { registry } = useCommandContext();
  return useSyncExternalStore(registry.subscribe, registry.getSnapshot);
}

export function useCommandPalette() {
  const { open, setOpen } = useCommandContext();
  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);
  return { open, setOpen, toggle };
}
