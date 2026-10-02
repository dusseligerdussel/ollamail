import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Todo } from "@/api/todos";
import { addDays, todayIn } from "@/lib/task-dates";
import { backend, json, mockFetch, problem } from "@/test/fetch";
import { messageId, testMailbox, testThread } from "@/test/mail";
import { renderApp } from "@/test/render-app";
import { testTodo, todoId } from "@/test/todos";
import { triageApi } from "@/test/triage";

const today = todayIn("UTC");

interface TodoBackend {
  todos?: Todo[];
  /** PATCH requests fail with this status. */
  patchStatus?: number;
  /** PATCH requests wait for this promise (to observe the optimistic state). */
  patchGate?: Promise<void>;
}

/** In-memory todo API on top of the default backend; records the requests that change data. */
function mockTodoApi({ todos = [], patchStatus, patchGate }: TodoBackend = {}) {
  const store = new Map(todos.map((todo) => [todo.id, todo]));
  const patches: { id: string; body: Record<string, unknown> }[] = [];
  const posts: Record<string, unknown>[] = [];
  const base = backend();
  const triage = triageApi();
  const fetchMock = mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/todos") {
      const statuses = url.searchParams.getAll("status");
      const message = url.searchParams.get("message_id");
      return json(
        [...store.values()].filter(
          (todo) =>
            (statuses.length === 0 || statuses.includes(todo.status)) &&
            (!message || todo.message_id === message),
        ),
      );
    }
    if (route === "POST /api/todos") {
      const body = (await request.json()) as Record<string, unknown>;
      posts.push(body);
      const created = testTodo(900 + posts.length, {
        title: String(body.title),
        message_id: (body.message_id as string | undefined) ?? null,
        is_manual: true,
      });
      store.set(created.id, created);
      return json(created, { status: 201 });
    }
    const patch = /^PATCH \/api\/todos\/([^/]+)$/.exec(route);
    if (patch?.[1]) {
      const body = (await request.json()) as Record<string, unknown>;
      patches.push({ id: patch[1], body });
      await patchGate;
      if (patchStatus) return problem(patchStatus);
      const current = store.get(patch[1]);
      if (!current) return problem(404);
      const updated = { ...current, ...body } as Todo;
      store.set(updated.id, updated);
      return json(updated);
    }
    if (route === "GET /api/mailboxes") return json([testMailbox()]);
    if (route === "GET /api/messages") return json({ items: [], total: 0, next_cursor: null });
    const thread = /^GET \/api\/messages\/([^/]+)\/thread$/.exec(route);
    if (thread?.[1]) return json(testThread(Number.parseInt(thread[1].slice(-12), 16)));
    if (route.startsWith("PATCH /api/messages/")) return json({});
    return (await triage.handle(request)) ?? base(request);
  });
  return { store, patches, posts, fetchMock };
}

/**
 * Types into a field. Focuses it directly: in jsdom every element has an empty box, so a
 * pointer click also lands on the shell's resize handle, which takes the focus.
 */
async function typeInto(field: HTMLElement, text: string) {
  act(() => field.focus());
  await userEvent.keyboard(text);
}

/** Activates a button with the keyboard (see `typeInto` for why not with a click). */
async function press(button: HTMLElement) {
  await typeInto(button, "{Enter}");
}

function section(name: string) {
  return screen.getByRole("region", { name: new RegExp(`^${name}`) });
}

function titlesIn(name: string) {
  return within(section(name))
    .getAllByRole("listitem")
    .map((item) => item.querySelector("[data-task-title]")?.textContent);
}

const sample = () => [
  testTodo(1, { title: "Send offer", due_date: addDays(today, -2), message_id: messageId(3) }),
  testTodo(2, { title: "Call the printer", due_date: today }),
  testTodo(3, { title: "Book train", due_date: addDays(today, 10) }),
  testTodo(4, { title: "Sort receipts", description: "Shoebox in the hallway" }),
  testTodo(5, { title: "Pay invoice", status: "done", completed_at: "2026-10-01T10:00:00Z" }),
];

describe("tasks page", () => {
  it("groups tasks into overdue, today, upcoming, no date and done", async () => {
    mockTodoApi({ todos: sample() });
    await renderApp("/tasks");

    expect(await screen.findByRole("region", { name: /^Overdue/ })).toBeInTheDocument();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Overdue1", "Today1", "Upcoming1", "No date1", "Done1"]);
    expect(titlesIn("Overdue")).toEqual(["Send offer"]);
    expect(titlesIn("Done")).toEqual(["Pay invoice"]);
    expect(screen.getByText("Shoebox in the hallway")).toBeInTheDocument();
    // Four open tasks next to the title.
    expect(screen.getByText("4")).toBeInTheDocument();
    // The overdue task links to its mail.
    expect(screen.getByRole("link", { name: "Open message of “Send offer”" })).toHaveAttribute(
      "href",
      `/inbox?message=${messageId(3)}`,
    );
  });

  it("shows an empty state with the field for a new task", async () => {
    mockTodoApi();
    await renderApp("/tasks");
    expect(await screen.findByText("No tasks")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "New task" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to inbox" })).toHaveAttribute("href", "/inbox");
  });

  it("checks off a task at once and keeps it done after the server answers", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const api = mockTodoApi({ todos: sample(), patchGate: gate });
    await renderApp("/tasks");

    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Mark “Call the printer” as done" }),
    );

    // Moved to "Done" before the server answered.
    await waitFor(() => expect(titlesIn("Done")).toContain("Call the printer"));
    expect(screen.queryByRole("region", { name: /^Today/ })).not.toBeInTheDocument();
    expect(api.patches).toEqual([{ id: todoId(2), body: { status: "done" } }]);
    await act(async () => release());
    await waitFor(() => expect(api.store.get(todoId(2))?.status).toBe("done"));
    expect(titlesIn("Done")).toContain("Call the printer");
  });

  it("rolls the change back when the server rejects it", async () => {
    mockTodoApi({ todos: sample(), patchStatus: 500 });
    await renderApp("/tasks");
    await userEvent.click(
      await screen.findByRole("checkbox", { name: "Mark “Book train” as done" }),
    );
    await waitFor(() => expect(titlesIn("Upcoming")).toEqual(["Book train"]));
    expect(await screen.findByText(/server error/)).toBeInTheDocument();
  });

  it("edits a title in place", async () => {
    const api = mockTodoApi({ todos: sample() });
    await renderApp("/tasks");
    await userEvent.click(await screen.findByRole("button", { name: "Book train" }));
    // The whole title is selected, typing replaces it.
    await typeInto(screen.getByRole("textbox", { name: "Title" }), "Book train to Leipzig{Enter}");

    expect(await screen.findByRole("button", { name: "Book train to Leipzig" })).toHaveFocus();
    expect(api.patches).toEqual([{ id: todoId(3), body: { title: "Book train to Leipzig" } }]);

    // Escape cancels without saving.
    await userEvent.click(screen.getByRole("button", { name: "Book train to Leipzig" }));
    await typeInto(screen.getByRole("textbox", { name: "Title" }), "x{Escape}");
    expect(screen.getByRole("button", { name: "Book train to Leipzig" })).toBeInTheDocument();
    expect(api.patches).toHaveLength(1);
  });

  it("changes the due date with quick choices and the date field", async () => {
    const api = mockTodoApi({ todos: sample() });
    await renderApp("/tasks");
    await screen.findByRole("region", { name: /^Overdue/ });

    // The first date button belongs to the overdue task.
    const [overdueDate] = screen.getAllByRole("button", { name: /^Due / });
    if (!overdueDate) throw new Error("no date button");
    await press(overdueDate);
    await press(await screen.findByRole("button", { name: /^Tomorrow/ }));
    expect(api.patches).toEqual([{ id: todoId(1), body: { due_date: addDays(today, 1) } }]);
    await waitFor(() => expect(titlesIn("Upcoming")).toEqual(["Send offer", "Book train"]));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // A task without date: set one through the date field.
    await press(within(section("No date")).getByRole("button", { name: "Set due date" }));
    const field = await screen.findByLabelText("Due date", { selector: "input" });
    fireEvent.change(field, { target: { value: "2027-01-15" } });
    await press(screen.getByRole("button", { name: "Set" }));
    expect(api.patches.at(-1)).toEqual({ id: todoId(4), body: { due_date: "2027-01-15" } });

    // Remove the date again.
    await press(await screen.findByRole("button", { name: /^Due .*2027/ }));
    await press(await screen.findByRole("button", { name: "No date" }));
    expect(api.patches.at(-1)).toEqual({ id: todoId(4), body: { due_date: null } });
  });

  it("creates a task with Enter and keeps the field ready for the next one", async () => {
    const api = mockTodoApi({ todos: sample() });
    await renderApp("/tasks");
    await screen.findByRole("region", { name: /^Overdue/ });

    await userEvent.keyboard("n");
    const field = screen.getByRole("textbox", { name: "New task" });
    expect(field).toHaveFocus();
    await userEvent.keyboard("Water the plants{Enter}");

    expect(field).toHaveValue("");
    expect(field).toHaveFocus();
    await waitFor(() => expect(titlesIn("No date")).toContain("Water the plants"));
    expect(api.posts).toEqual([{ title: "Water the plants", priority: "normal" }]);
  });

  it("dismisses a task and brings it back with undo", async () => {
    const api = mockTodoApi({ todos: sample() });
    await renderApp("/tasks");
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss “Book train”" }));

    await waitFor(() =>
      expect(screen.queryByRole("region", { name: /^Upcoming/ })).not.toBeInTheDocument(),
    );
    await press(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(titlesIn("Upcoming")).toEqual(["Book train"]));
    expect(api.patches.map((patch) => patch.body)).toEqual([
      { status: "dismissed" },
      { status: "open" },
    ]);
  });

  it("works with the keyboard: j/k, x, d and o", async () => {
    const api = mockTodoApi({ todos: sample() });
    const { router } = await renderApp("/tasks");
    await screen.findByRole("region", { name: /^Overdue/ });

    await userEvent.keyboard("jj");
    expect(screen.getByRole("button", { name: "Call the printer" })).toHaveFocus();
    await userEvent.keyboard("x");
    expect(api.patches).toEqual([{ id: todoId(2), body: { status: "done" } }]);

    await userEvent.keyboard("k");
    expect(screen.getByRole("button", { name: "Send offer" })).toHaveFocus();
    // Enter on the focused title edits it; Escape returns to the title.
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("textbox", { name: "Title" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Send offer" })).toHaveFocus();

    await userEvent.keyboard("d");
    expect(await screen.findByRole("dialog", { name: "Due date" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await userEvent.keyboard("o");
    await waitFor(() => expect(router.state.location.pathname).toBe("/inbox"));
    expect(router.state.location.search).toEqual({ message: messageId(3) });
  });
});

describe("tasks of a message", () => {
  it("lists the tasks of the opened mail and adds one linked to it", async () => {
    const id = messageId(3);
    const api = mockTodoApi({
      todos: [
        testTodo(1, { title: "Send offer", due_date: today, message_id: id }),
        testTodo(2, { title: "Other mail", message_id: messageId(4) }),
      ],
    });
    await renderApp(`/inbox?message=${id}`);

    const tasks = await screen.findByRole("region", { name: "Tasks from this message" });
    expect(within(tasks).getAllByRole("listitem")).toHaveLength(1);
    expect(within(tasks).getByRole("button", { name: "Send offer" })).toBeInTheDocument();
    // No link back to the mail that is already open.
    expect(within(tasks).queryByRole("link", { name: /Open message/ })).not.toBeInTheDocument();

    await userEvent.click(
      within(tasks).getByRole("checkbox", { name: "Mark “Send offer” as done" }),
    );
    expect(api.patches).toEqual([{ id: todoId(1), body: { status: "done" } }]);

    await typeInto(within(tasks).getByRole("textbox", { name: "New task" }), "Reply{Enter}");
    expect(api.posts).toEqual([{ title: "Reply", message_id: id, priority: "normal" }]);
    await waitFor(() => expect(within(tasks).getAllByRole("listitem")).toHaveLength(2));
  });

  it("offers to add a task when the mail has none", async () => {
    const id = messageId(5);
    const api = mockTodoApi();
    await renderApp(`/inbox?message=${id}`);

    await userEvent.click(await screen.findByRole("button", { name: "Add task to this message" }));
    await userEvent.keyboard("Ask for the agenda{Enter}");

    expect(api.posts).toEqual([
      { title: "Ask for the agenda", message_id: id, priority: "normal" },
    ]);
    expect(
      await screen.findByRole("region", { name: "Tasks from this message" }),
    ).toBeInTheDocument();
  });
});
