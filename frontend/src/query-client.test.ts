import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import "@/i18n";

import { ApiError } from "@/api/errors";

import { createQueryClient } from "./query-client";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

afterEach(() => {
  vi.mocked(toast.error).mockClear();
});

function failing(error: unknown) {
  return () => Promise.reject(error);
}

describe("createQueryClient error handling", () => {
  it("does not toast a first load failure (shown inline)", async () => {
    const client = createQueryClient();

    await client.prefetchQuery({ queryKey: ["a"], queryFn: failing(new ApiError(404)) });

    expect(toast.error).not.toHaveBeenCalled();
  });

  it("toasts a failed background refresh", async () => {
    const client = createQueryClient();
    client.setQueryData(["a"], "cached");

    await client
      .fetchQuery({ queryKey: ["a"], queryFn: failing(new ApiError(404)), staleTime: 0 })
      .catch(() => {});

    expect(toast.error).toHaveBeenCalledWith(
      "The requested item was not found.",
      expect.objectContaining({ id: "The requested item was not found." }),
    );
  });

  it("toasts failed mutations unless disabled", async () => {
    const client = createQueryClient();
    const error = new ApiError(409, { title: "Conflict", status: 409, request_id: "r1" });

    await client
      .getMutationCache()
      .build(client, { mutationFn: failing(error) })
      .execute(undefined)
      .catch(() => {});
    await client
      .getMutationCache()
      .build(client, { mutationFn: failing(error), meta: { errorToast: false } })
      .execute(undefined)
      .catch(() => {});

    expect(toast.error).toHaveBeenCalledOnce();
    expect(toast.error).toHaveBeenCalledWith(
      "The item was changed in the meantime. Reload and try again.",
      expect.objectContaining({ description: "Request ID: r1" }),
    );
  });

  it("never toasts 401 (the client redirects to the login page)", async () => {
    const client = createQueryClient();

    await client
      .getMutationCache()
      .build(client, { mutationFn: failing(new ApiError(401)) })
      .execute(undefined)
      .catch(() => {});

    expect(toast.error).not.toHaveBeenCalled();
  });

  it("does not retry client errors", async () => {
    const client = createQueryClient();
    const queryFn = vi.fn(failing(new ApiError(404)));

    await client.prefetchQuery({ queryKey: ["b"], queryFn, retryDelay: 0 });

    expect(queryFn).toHaveBeenCalledOnce();
  });

  it("retries server and network errors", async () => {
    const client = createQueryClient();
    const queryFn = vi.fn(failing(new ApiError(0)));

    await client.prefetchQuery({ queryKey: ["c"], queryFn, retryDelay: 0 });

    expect(queryFn).toHaveBeenCalledTimes(3);
  });
});
