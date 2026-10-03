import { describe, expect, it } from "vitest";

import { allMailboxesQueryOptions, mailboxesQueryOptions } from "@/api/mail";
import { testMailbox } from "@/test/mail";

describe("mailbox queries", () => {
  it("leave mailboxes being removed out of everything that reads mail", () => {
    const active = testMailbox();
    const deleting = testMailbox({
      id: "0199b000-0000-7000-8000-000000000002",
      status: { ...testMailbox().status, phase: "deleting" },
    });

    expect(mailboxesQueryOptions.select?.([active, deleting])).toEqual([active]);
    // The mailbox settings show them, with their status.
    expect(allMailboxesQueryOptions.select).toBeUndefined();
    expect(allMailboxesQueryOptions.queryKey).toEqual(mailboxesQueryOptions.queryKey);
  });
});
