import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { backend, json, mockFetch, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";
import { testExportTarget, todoExportApi, withExportApi } from "@/test/todo-export";
import { testTodo } from "@/test/todos";

function setup(options: Parameters<typeof todoExportApi>[0] = {}) {
  const api = todoExportApi(options);
  mockFetch(withExportApi(api, backend({ user: testUser })));
  return api;
}

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("task export settings", () => {
  it("is linked from the settings", async () => {
    setup();
    await renderApp("/settings");

    expect(screen.getByRole("link", { name: /export tasks/i })).toHaveAttribute(
      "href",
      "/settings/task-export",
    );
  });

  it("says when the administrator has not enabled the export", async () => {
    setup({ sinks: [] });
    await renderApp("/settings/task-export");

    expect(await screen.findByText("Export not enabled")).toBeInTheDocument();
    expect(screen.queryByLabelText(/server url/i)).not.toBeInTheDocument();
  });

  it("connects: credentials, list, mode", async () => {
    const api = setup();
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    // What leaves the instance is stated before anything is sent.
    expect(await screen.findByText(/sends the title, description, due date/i)).toBeVisible();
    expect(screen.getByRole("radio", { name: /caldav/i })).toBeChecked();
    fill(/server url/i, " https://cloud.example.org/remote.php/dav ");
    fill(/user name/i, "erika");
    fill(/^password/i, "app-password");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    const list = await screen.findByLabelText("List");
    fireEvent.change(list, { target: { value: "/calendars/erika/work/" } });
    await user.click(screen.getByRole("radio", { name: /manual/i }));
    await user.click(screen.getByRole("button", { name: "Turn on export" }));

    expect(await screen.findByText("Work")).toBeInTheDocument();
    expect(api.requests).toEqual([
      {
        method: "POST",
        path: "/api/todo-export/lists",
        body: {
          sink: "caldav",
          url: "https://cloud.example.org/remote.php/dav",
          username: "erika",
          password: "app-password",
        },
      },
      {
        method: "PUT",
        path: "/api/todo-export",
        body: {
          sink: "caldav",
          url: "https://cloud.example.org/remote.php/dav",
          username: "erika",
          password: "app-password",
          list_id: "/calendars/erika/work/",
          mode: "manual",
        },
      },
    ]);
    expect(screen.getByRole("radio", { name: /manual/i })).toBeChecked();
  });

  it("shows why the connection failed", async () => {
    setup({ listsError: "auth_failed" });
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    await screen.findByLabelText(/server url/i);
    fill(/server url/i, "https://cloud.example.org/");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server rejected the user name or password.",
    );
    expect(screen.queryByLabelText("List")).not.toBeInTheDocument();
  });

  it("shows the connection and its state", async () => {
    setup({
      target: testExportTarget({
        last_error: "unavailable",
        counts: { synced: 3, pending: 1, error: 2, removed: 0 },
      }),
    });
    await renderApp("/settings/task-export");

    const connection = await screen.findByRole("region", { name: "Connection" });
    expect(within(connection).getByText("https://cloud.example.org/remote.php/dav")).toBeVisible();
    expect(within(connection).getByText("Tasks", { selector: "dd" })).toBeVisible();
    expect(within(connection).getByText("The server cannot be reached.")).toBeVisible();
    expect(within(connection).getByText(/3 exported · 1 waiting/)).toBeVisible();
    expect(within(connection).getByText(/2 failed/)).toBeVisible();
    // Never a password field outside the form.
    expect(screen.queryByLabelText(/^password/i)).not.toBeInTheDocument();
  });

  it("changes the mode, syncs and disconnects", async () => {
    const api = setup({ target: testExportTarget() });
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    await user.click(await screen.findByRole("radio", { name: /manual/i }));
    await waitFor(() => expect(screen.getByRole("radio", { name: /manual/i })).toBeChecked());
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/tasks already exported stay in the list/i);
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));

    expect(await screen.findByLabelText(/server url/i)).toBeInTheDocument();
    expect(api.requests.map((request) => `${request.method} ${request.path}`)).toEqual([
      "PATCH /api/todo-export",
      "POST /api/todo-export/sync",
      "DELETE /api/todo-export",
    ]);
  });

  it("keeps the stored password when only the list changes", async () => {
    const api = setup({ target: testExportTarget() });
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    await user.click(await screen.findByRole("button", { name: "Change" }));
    expect(screen.getByText("Leave empty to keep the stored password.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await user.click(await screen.findByRole("button", { name: "Save" }));

    await screen.findByRole("region", { name: "Connection" });
    expect(api.requests[0]?.body).toMatchObject({ password: null });
  });
});

describe("export state in the task list", () => {
  function tasksBackend(options: Parameters<typeof todoExportApi>[0], todos = [testTodo(1)]) {
    const api = todoExportApi(options);
    const exported: string[] = [];
    const base = backend({ user: testUser });
    mockFetch(async (request) => {
      const { pathname, searchParams } = new URL(request.url);
      const route = `${request.method} ${pathname}`;
      const single = /^POST \/api\/todo-export\/todos\/([^/]+)$/.exec(route);
      if (single?.[1]) {
        exported.push(single[1]);
        const todo = todos.find((item) => item.id === single[1]);
        return json({
          ...todo,
          export_state: { sink: "caldav", state: "pending", synced_at: null, error: null },
        });
      }
      if (route === "GET /api/todos") {
        const statuses = searchParams.getAll("status");
        return json(todos.filter((todo) => statuses.includes(todo.status)));
      }
      return (await api.handle(request)) ?? base(request);
    });
    return { exported };
  }

  it("marks exported and failed tasks", async () => {
    tasksBackend({ target: testExportTarget() }, [
      testTodo(1, {
        export_state: {
          sink: "caldav",
          state: "synced",
          synced_at: "2026-10-07T09:00:00Z",
          error: null,
        },
      }),
      testTodo(2, {
        export_state: { sink: "caldav", state: "error", synced_at: null, error: "http_400" },
      }),
    ]);
    await renderApp("/tasks");

    expect(await screen.findByRole("img", { name: "Exported" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Export failed" })).toBeInTheDocument();
    // Automatic mode: nothing to export by hand.
    expect(screen.queryByRole("button", { name: /^export “/i })).not.toBeInTheDocument();
  });

  it("exports a single task in manual mode", async () => {
    const { exported } = tasksBackend({ target: testExportTarget({ mode: "manual" }) });
    const user = userEvent.setup();
    await renderApp("/tasks");

    await user.click(await screen.findByRole("button", { name: "Export “Task 1” to “Tasks”" }));

    await waitFor(() => expect(exported).toEqual([testTodo(1).id]));
  });

  it("offers no export without a connection", async () => {
    tasksBackend({ target: null });
    await renderApp("/tasks");

    await screen.findByText("Task 1");
    expect(screen.queryByRole("button", { name: /^export “/i })).not.toBeInTheDocument();
  });
});
