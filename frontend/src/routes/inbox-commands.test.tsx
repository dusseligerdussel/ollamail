import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import type { Command } from "@/lib/commands";
import { backend, json, mockFetch } from "@/test/fetch";
import { testMailbox, testMessage } from "@/test/mail";
import { renderApp } from "@/test/render-app";

/** Every command list the inbox registers, in order (#115). */
const inboxCommandLists = vi.hoisted(() => [] as (readonly Command[])[]);

vi.mock("@/components/command-palette/command-provider", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/components/command-palette/command-provider")>();
  return {
    ...actual,
    useCommands: (commands: readonly Command[]) => {
      const isInbox = commands.some((command) => command.id === "mail.filterUnread");
      if (isInbox && inboxCommandLists.at(-1) !== commands) inboxCommandLists.push(commands);
      actual.useCommands(commands);
    },
  };
});

// jsdom has no layout; the virtualised list needs a viewport height to render rows.
const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 720 });
});
afterAll(() => {
  if (offsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", offsetHeight);
});

describe("inbox commands", () => {
  it("keeps its commands when the next page loads", async () => {
    let releaseSecondPage = () => {};
    const secondPage = new Promise<void>((resolve) => {
      releaseSecondPage = resolve;
    });
    const base = backend();
    mockFetch(async (request) => {
      const url = new URL(request.url);
      const route = `${request.method} ${url.pathname}`;
      if (route === "GET /api/mailboxes") return json([testMailbox()]);
      if (route === "GET /api/messages") {
        if (url.searchParams.get("cursor") === "page-2") {
          await secondPage;
          return json({ items: [testMessage(3), testMessage(4)], total: 4, next_cursor: null });
        }
        return json({ items: [testMessage(1), testMessage(2)], total: 4, next_cursor: "page-2" });
      }
      return base(request);
    });
    await renderApp("/inbox");
    await screen.findByText("Sender 1");

    // j selects the first row, which adds the read state toggle.
    fireEvent.keyDown(document.body, { key: "j" });
    await waitFor(() =>
      expect(inboxCommandLists.at(-1)?.map((command) => command.id)).toContain("mail.toggleUnread"),
    );
    const registered = inboxCommandLists.length;

    releaseSecondPage();
    expect(await screen.findByText("Sender 4")).toBeInTheDocument();
    expect(inboxCommandLists).toHaveLength(registered);
  });
});
