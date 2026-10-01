import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { createRegistry, type Registry } from "@/lib/registry";
import { createShortcutDispatcher, type Shortcut } from "@/lib/shortcuts";

const ShortcutContext = createContext<Registry<Shortcut> | null>(null);

/** Owns the shortcut registry and the single global `keydown` listener. */
export function ShortcutProvider({ children }: { children: ReactNode }) {
  const [registry] = useState(() => createRegistry<Shortcut>());

  useEffect(() => {
    const dispatch = createShortcutDispatcher(registry);
    window.addEventListener("keydown", dispatch);
    return () => window.removeEventListener("keydown", dispatch);
  }, [registry]);

  return <ShortcutContext value={registry}>{children}</ShortcutContext>;
}

function useShortcutRegistry() {
  const registry = useContext(ShortcutContext);
  if (!registry) throw new Error("Shortcut hooks must be used within <ShortcutProvider>");
  return registry;
}

export interface UseShortcutOptions extends Omit<Shortcut, "handler"> {
  /** Set to false to unregister the shortcut temporarily. */
  enabled?: boolean;
}

/**
 * Registers a keyboard shortcut while the calling component is mounted. The handler may change
 * on every render without re-registering.
 */
export function useShortcut(
  { enabled = true, ...shortcut }: UseShortcutOptions,
  handler: Shortcut["handler"],
) {
  const registry = useShortcutRegistry();
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  const { id, keys, description, group, allowInInput, hidden } = shortcut;
  useEffect(() => {
    if (!enabled) return;
    return registry.register({
      id,
      keys,
      description,
      group,
      allowInInput,
      hidden,
      handler: (event) => handlerRef.current(event),
    });
  }, [registry, enabled, id, keys, description, group, allowInInput, hidden]);
}

/** All currently registered shortcuts, e.g. for the overview. */
export function useRegisteredShortcuts(): readonly Shortcut[] {
  const registry = useShortcutRegistry();
  return useSyncExternalStore(registry.subscribe, registry.getSnapshot);
}
