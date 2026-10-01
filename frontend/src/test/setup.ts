import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { mockFetch } from "./fetch";
import { setPrefersDark, setViewportWidth } from "./media";

// jsdom does not implement scrolling; the router's scroll restoration calls it.
window.scrollTo = vi.fn() as typeof window.scrollTo;
Element.prototype.scrollIntoView = vi.fn();

// Used by cmdk and react-resizable-panels.
window.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// Radix UI expects pointer capture APIs that jsdom lacks.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};

beforeEach(() => {
  mockFetch();
  setViewportWidth(1440);
  setPrefersDark(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
});
