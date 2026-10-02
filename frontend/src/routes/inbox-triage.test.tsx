import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { MessageSummary, Thread } from "@/api/mail";
import type { Triage, TriagedMessage } from "@/api/triage";
import { backend, json, mockFetch } from "@/test/fetch";
import { messageId, testMailbox, testMessage, testThread } from "@/test/mail";
import { renderApp } from "@/test/render-app";
import { categoryId, testTriage, testTriagedMessage, triageApi } from "@/test/triage";

function mockApi({
  messages = [] as MessageSummary[],
  threads = {} as Record<string, Thread>,
  results = [] as Triage[],
  inbox = [] as TriagedMessage[],
} = {}) {
  const triage = triageApi({ results, inbox });
  const base = backend();
  mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/mailboxes") return json([testMailbox()]);
    if (route === "GET /api/messages") {
      return json({ items: messages, total: messages.length, next_cursor: null });
    }
    const thread = /^GET \/api\/messages\/([^/]+)\/thread$/.exec(route);
    if (thread?.[1] && threads[thread[1]]) return json(threads[thread[1]]);
    return (await triage.handle(request)) ?? base(request);
  });
  return triage;
}

// jsdom has no layout; the virtualised list needs a viewport height to render rows.
const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 720 });
});
afterAll(() => {
  if (offsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", offsetHeight);
});

describe("triage in the inbox", () => {
  it("labels list rows with their category, loaded in one request", async () => {
    const triage = mockApi({
      messages: [testMessage(1), testMessage(2), testMessage(3)],
      results: [
        testTriage(messageId(1), { category_id: categoryId(2), priority: 1 }),
        testTriage(messageId(2), { category_id: categoryId(9) }),
      ],
    });
    await renderApp("/inbox");
    const list = await screen.findByRole("list", { name: "Messages" });
    const rows = within(list).getAllByRole("listitem");

    expect(
      await within(rows[0] as HTMLElement).findByText("Category: Action required, high priority"),
    ).toBeInTheDocument();
    expect(
      within(rows[1] as HTMLElement).getByText("Category: Project Apollo"),
    ).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).queryByTestId("triage-label")).not.toBeInTheDocument();
    expect(triage.batches).toEqual([[messageId(1), messageId(2), messageId(3)]]);
  });

  it("explains the category above the message", async () => {
    const id = messageId(1);
    mockApi({
      messages: [testMessage(1)],
      threads: { [id]: testThread(1) },
      results: [testTriage(id)],
    });
    await renderApp(`/inbox?message=${id}`);
    const line = await screen.findByTestId("triage-reason");
    await waitFor(() =>
      expect(line).toHaveTextContent(
        "Categorized as Action required. The sender asks for a decision.",
      ),
    );
  });

  it("names the rule that decided without the model", async () => {
    const id = messageId(1);
    mockApi({
      messages: [testMessage(1)],
      threads: { [id]: testThread(1) },
      results: [
        testTriage(id, {
          category_id: categoryId(5),
          source: "rule",
          rule: "list_unsubscribe",
          reason: null,
        }),
      ],
    });
    await renderApp(`/inbox?message=${id}`);
    const line = await screen.findByTestId("triage-reason");
    await waitFor(() =>
      expect(line).toHaveTextContent(
        "Categorized as Newsletter because the message contains an unsubscribe link.",
      ),
    );
  });

  it("corrects the category with c and a digit", async () => {
    const id = messageId(1);
    const triage = mockApi({
      messages: [testMessage(1)],
      threads: { [id]: testThread(1) },
      results: [testTriage(id, { priority: 1 })],
    });
    await renderApp(`/inbox?message=${id}`);
    const line = await screen.findByTestId("triage-reason");
    await waitFor(() => expect(line).toHaveTextContent("Action required"));

    // Two key presses: c opens the picker, 4 picks the fourth category (Info).
    fireEvent.keyDown(document.body, { key: "c" });
    const picker = await screen.findByRole("dialog", { name: "Categorize as" });
    fireEvent.keyDown(within(picker).getByRole("combobox"), { key: "4" });

    await waitFor(() =>
      expect(triage.corrections).toEqual([
        { id, body: { category_id: categoryId(4), priority: 1 } },
      ]),
    );
    await waitFor(() => expect(line).toHaveTextContent("You categorized this as Info."));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("corrects the category from the command palette and by click", async () => {
    const id = messageId(1);
    const triage = mockApi({
      messages: [testMessage(1)],
      threads: { [id]: testThread(1) },
      results: [testTriage(id)],
    });
    await renderApp(`/inbox?message=${id}`);
    await waitFor(() =>
      expect(screen.getByTestId("triage-reason")).toHaveTextContent("Action required"),
    );

    fireEvent.keyDown(document.body, { key: "k", ctrlKey: true });
    const palette = await screen.findByRole("dialog", { name: "Command menu" });
    await userEvent.type(within(palette).getByRole("combobox"), "Categorize as Waiting");
    await userEvent.click(
      within(palette).getByRole("option", { name: /Categorize as Waiting for/ }),
    );
    await waitFor(() => expect(triage.corrections.at(-1)?.body.category_id).toBe(categoryId(3)));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const reason = screen.getByTestId("triage-reason");
    // The line has a menu (keyboard here; jsdom lacks Radix's pointer events).
    (await within(reason).findByRole("button", { name: "Change category" })).focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Project Apollo" }));
    await waitFor(() => expect(triage.corrections.at(-1)?.body.category_id).toBe(categoryId(9)));
  });

  it("groups the inbox by category, highest priority first", async () => {
    const triage = mockApi({
      messages: [testMessage(1), testMessage(2), testMessage(3)],
      inbox: [
        testTriagedMessage(2, 2, 1),
        testTriagedMessage(1, 2, 3),
        testTriagedMessage(3, null, null),
      ],
    });
    const { router } = await renderApp("/inbox");
    await screen.findByText("Sender 1");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "View" }), "By category");

    expect(await screen.findByRole("heading", { level: 2, name: "Action required" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "Uncategorized" })).toBeVisible();
    const list = screen.getByRole("list", { name: "Messages" });
    expect(
      within(list)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual([
      "Action required",
      expect.stringContaining("Sender 2"),
      expect.stringContaining("Sender 1"),
      "Uncategorized",
      expect.stringContaining("Sender 3"),
    ]);
    expect(router.state.location.search).toMatchObject({ category: "all" });
    // The groups name the category; the row labels would repeat it.
    expect(within(list).queryByTestId("triage-label")).not.toBeInTheDocument();
    expect(triage.inboxQueries.at(-1)?.has("category")).toBe(false);

    // A heading filters to its group.
    await userEvent.click(screen.getByRole("link", { name: "Uncategorized" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ category: "none" }));
    await waitFor(() => expect(screen.queryByText("Sender 1")).not.toBeInTheDocument());
    expect(triage.inboxQueries.at(-1)?.get("category")).toBe("none");
  });
});
