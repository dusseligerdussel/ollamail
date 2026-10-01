import { useCallback, useSyncExternalStore } from "react";

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches);
}

/** Breakpoints of the app shell (Tailwind `md` and `lg`). */
export const mediaQueries = {
  /** Navigation is shown as a sidebar instead of a bottom bar. */
  sidebar: "(min-width: 768px)",
  /** List and detail are shown side by side instead of stacked. */
  split: "(min-width: 1024px)",
  /**
   * A keyboard is likely: wide viewport and a precise pointer. Elsewhere (phones, touch tablets)
   * shortcut hints and the shortcut overview are not shown.
   */
  keyboard: "(min-width: 768px) and (any-pointer: fine)",
} as const;
