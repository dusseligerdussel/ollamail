import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

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
});
