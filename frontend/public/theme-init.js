// Applies the stored theme before the first paint to avoid a flash of the wrong theme.
// Loaded as a blocking script from index.html; keep in sync with src/lib/theme.ts.
(() => {
  let theme = "system";
  try {
    const stored = window.localStorage.getItem("ollamail.theme");
    if (stored === "light" || stored === "dark") theme = stored;
  } catch {
    // Storage unavailable: follow the system preference.
  }
  const dark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const root = document.documentElement;
  root.classList.add(dark ? "dark" : "light");
  root.style.colorScheme = dark ? "dark" : "light";
})();
