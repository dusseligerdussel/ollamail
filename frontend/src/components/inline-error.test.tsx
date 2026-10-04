import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import i18n from "@/i18n";

import { InlineError } from "./inline-error";

beforeAll(async () => {
  await i18n.changeLanguage("en");
});

describe("InlineError", () => {
  it("offers no retry without onRetry", () => {
    render(<InlineError error={new Error("boom")} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("retries on click and is disabled while retrying", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<InlineError error={new Error("boom")} onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();

    rerender(<InlineError error={new Error("boom")} onRetry={onRetry} retrying />);
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();
  });

  it("waits for Retry-After before offering the retry", () => {
    vi.useFakeTimers();
    try {
      const error = new ApiError(429, { status: 429 }, { retryAfter: 2 });
      render(<InlineError error={error} onRetry={vi.fn()} />);
      expect(screen.getByRole("button", { name: "Try again in 2 s" })).toBeDisabled();
      act(() => vi.advanceTimersByTime(1000));
      expect(screen.getByRole("button", { name: "Try again in 1 s" })).toBeDisabled();
      act(() => vi.advanceTimersByTime(1000));
      expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });
});
