export const themes = ["light", "dark", "system"] as const;
export type Theme = (typeof themes)[number];
export type ResolvedTheme = Exclude<Theme, "system">;

// Keep in sync with public/theme-init.js, which applies the theme before the first paint.
export const themeStorageKey = "ollamail.theme";

const darkQuery = "(prefers-color-scheme: dark)";

export function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (themes as readonly string[]).includes(value);
}

export function readStoredTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(themeStorageKey);
    return isTheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function storeTheme(theme: Theme) {
  try {
    if (theme === "system") {
      window.localStorage.removeItem(themeStorageKey);
    } else {
      window.localStorage.setItem(themeStorageKey, theme);
    }
  } catch {
    // Storage can be unavailable (private mode); the choice then lasts for this session only.
  }
}

export function systemPrefersDark(): boolean {
  return window.matchMedia?.(darkQuery).matches ?? false;
}

export function resolveTheme(theme: Theme, prefersDark = systemPrefersDark()): ResolvedTheme {
  if (theme === "system") return prefersDark ? "dark" : "light";
  return theme;
}

export function subscribeToSystemTheme(onChange: () => void): () => void {
  const query = window.matchMedia?.(darkQuery);
  if (!query) return () => {};
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/** Applies the theme to <html> without animating every colour transition. */
export function applyTheme(resolved: ResolvedTheme) {
  const root = document.documentElement;
  if (root.classList.contains(resolved) && root.style.colorScheme === resolved) return;

  root.classList.add("theme-switching");
  root.classList.toggle("dark", resolved === "dark");
  root.classList.toggle("light", resolved === "light");
  root.style.colorScheme = resolved;
  // Force a style flush, then re-enable transitions.
  void window.getComputedStyle(root).opacity;
  root.classList.remove("theme-switching");
}
