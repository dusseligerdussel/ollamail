import { describe, expect, it, vi } from "vitest";

import { retryFailed } from "./retry-failed";

function query(isError: boolean, isFetching = false) {
  return { isError, isFetching, refetch: vi.fn(() => Promise.resolve()) };
}

describe("retryFailed", () => {
  it("refetches only the failed queries", async () => {
    const ok = query(false);
    const failed = query(true);
    await retryFailed(ok, failed).onRetry();
    expect(ok.refetch).not.toHaveBeenCalled();
    expect(failed.refetch).toHaveBeenCalledOnce();
  });

  it("is retrying while any query fetches", () => {
    expect(retryFailed(query(true), query(false)).retrying).toBe(false);
    expect(retryFailed(query(true, true), query(false)).retrying).toBe(true);
  });
});
