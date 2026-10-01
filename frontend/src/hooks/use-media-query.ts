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
} as const;
