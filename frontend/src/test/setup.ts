import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom does not implement scrolling; the router's scroll restoration calls it.
window.scrollTo = vi.fn() as typeof window.scrollTo;

afterEach(() => {
  cleanup();
});
