import { describe, expect, it, vi } from "vitest";

import { testTriage } from "@/test/triage";

import { createTriageLoader, TRIAGE_BATCH_SIZE, type Triage } from "./triage";

const id = (index: number) => `0199c000-0000-7000-8000-${String(index).padStart(12, "0")}`;

describe("createTriageLoader", () => {
  it("loads the messages requested in the same tick with one request", async () => {
    const fetchMany = vi.fn(async (ids: string[]) => ids.slice(1).map((m) => testTriage(m)));
    const load = createTriageLoader(fetchMany);

    const results = await Promise.all([load(id(1)), load(id(2)), load(id(3)), load(id(2))]);

    expect(fetchMany).toHaveBeenCalledExactlyOnceWith([id(1), id(2), id(3)]);
    // Not triaged yet: null.
    expect(results.map((result) => result?.message_id ?? null)).toEqual([
      null,
      id(2),
      id(3),
      id(2),
    ]);
  });

  it("splits large batches and starts a new batch on the next tick", async () => {
    const fetchMany = vi.fn(async (ids: string[]) => ids.map((m) => testTriage(m)));
    const load = createTriageLoader(fetchMany);
    const ids = Array.from({ length: TRIAGE_BATCH_SIZE + 1 }, (_, index) => id(index));

    await Promise.all(ids.map(load));
    await load(id(999));

    expect(fetchMany.mock.calls.map(([batch]) => batch.length)).toEqual([TRIAGE_BATCH_SIZE, 1, 1]);
  });

  it("rejects the waiting requests when loading fails", async () => {
    const error = new Error("offline");
    const load = createTriageLoader(async (): Promise<Triage[]> => {
      throw error;
    });
    await expect(Promise.all([load(id(1)), load(id(2))])).rejects.toBe(error);
  });
});
