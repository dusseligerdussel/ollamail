import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { themeStorageKey } from "@/lib/theme";
import { setPrefersDark } from "@/test/media";

import { ThemeProvider, useTheme } from "./theme-provider";

function ThemeProbe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <output aria-label="theme">{`${theme}:${resolvedTheme}`}</output>
      <button type="button" onClick={() => setTheme("dark")}>
        dark
      </button>
      <button type="button" onClick={() => setTheme("light")}>
        light
      </button>
      <button type="button" onClick={() => setTheme("system")}>
        system
      </button>
    </div>
  );
}

const root = document.documentElement;

describe("ThemeProvider", () => {
  it("applies and stores an explicit theme", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );
    expect(screen.getByLabelText("theme")).toHaveTextContent("system:light");
    expect(root).not.toHaveClass("dark");

    await user.click(screen.getByRole("button", { name: "dark" }));
    expect(root).toHaveClass("dark");
    expect(root.style.colorScheme).toBe("dark");
    expect(window.localStorage.getItem(themeStorageKey)).toBe("dark");

    await user.click(screen.getByRole("button", { name: "light" }));
    expect(root).not.toHaveClass("dark");
    expect(window.localStorage.getItem(themeStorageKey)).toBe("light");
  });

  it("follows the system preference in system mode", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(themeStorageKey, "light");
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole("button", { name: "system" }));
    expect(window.localStorage.getItem(themeStorageKey)).toBeNull();

    act(() => setPrefersDark(true));
    expect(screen.getByLabelText("theme")).toHaveTextContent("system:dark");
    expect(root).toHaveClass("dark");

    act(() => setPrefersDark(false));
    expect(root).not.toHaveClass("dark");
  });

  it("restores the stored theme on start", () => {
    window.localStorage.setItem(themeStorageKey, "dark");
    render(
      <ThemeProvider>
        <ThemeProbe />
      </ThemeProvider>,
    );
    expect(screen.getByLabelText("theme")).toHaveTextContent("dark:dark");
    expect(root).toHaveClass("dark");
  });
});

describe("theme-init.js", () => {
  const script = readFileSync(
    path.resolve(import.meta.dirname, "../../public/theme-init.js"),
    "utf8",
  );
  const run = () => new Function(script)();

  it("applies the stored theme before React renders", () => {
    window.localStorage.setItem(themeStorageKey, "dark");
    run();
    expect(root).toHaveClass("dark");
    expect(root.style.colorScheme).toBe("dark");
  });

  it("falls back to the system preference", () => {
    setPrefersDark(true);
    run();
    expect(root).toHaveClass("dark");
  });
});
