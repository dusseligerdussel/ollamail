import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { pageNavigation } from "@/api/auth";
import { backend, mockFetch, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";
import { testMsTodoTarget, todoExportApi, withExportApi } from "@/test/todo-export";

function setup(options: Parameters<typeof todoExportApi>[0] = {}) {
  const api = todoExportApi({ sinks: ["caldav", "mstodo"], ...options });
  mockFetch(withExportApi(api, backend({ user: testUser })));
  return api;
}

describe("task export to Microsoft To Do", () => {
  it("signs in with Microsoft instead of asking for a server and password", async () => {
    const api = setup();
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    await user.click(await screen.findByRole("radio", { name: /microsoft to do/i }));

    expect(screen.queryByLabelText(/server url/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^password/i)).not.toBeInTheDocument();
    // What leaves the instance, and to whom.
    expect(screen.getByText(/plus a link to the message, to Microsoft To Do/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Sign in with Microsoft" }));

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://login.example.com/authorize?state=s"),
    );
    expect(api.requests).toEqual([
      {
        method: "POST",
        path: "/api/todo-export/mstodo/connect",
        body: { return_to: "/settings/task-export" },
      },
    ]);
    assign.mockRestore();
  });

  it("picks list and mode after the sign-in", async () => {
    const api = setup({ msTodoSignedIn: true });
    const user = userEvent.setup();
    await renderApp("/settings/task-export?mstodo=connected");

    expect(await screen.findByText("Signed in as erika@example.com")).toBeVisible();
    expect(screen.getByRole("radio", { name: /microsoft to do/i })).toBeChecked();
    fireEvent.change(screen.getByLabelText("List"), { target: { value: "AQMkADAw-work" } });
    await user.click(screen.getByRole("radio", { name: /manual/i }));
    await user.click(screen.getByRole("button", { name: "Turn on export" }));

    expect(await screen.findByText("Work")).toBeInTheDocument();
    expect(screen.getByText("Account")).toBeInTheDocument();
    expect(screen.queryByText("Server URL")).not.toBeInTheDocument();
    expect(api.requests).toContainEqual({
      method: "PUT",
      path: "/api/todo-export/mstodo",
      body: { list_id: "AQMkADAw-work", mode: "manual" },
    });
  });

  it("reports a failed sign-in", async () => {
    setup();
    await renderApp("/settings/task-export?mstodo=error&reason=consent_denied");

    expect(await screen.findByText("Microsoft sign-in failed")).toBeInTheDocument();
    expect(
      screen.getByText("The sign-in was cancelled or access was not allowed."),
    ).toBeInTheDocument();
  });

  it("asks to sign in again when Microsoft rejects the stored access", async () => {
    setup({ target: testMsTodoTarget({ last_error: "auth_failed" }) });
    await renderApp("/settings/task-export");

    expect(
      await screen.findByText("Microsoft rejected the access. Sign in again."),
    ).toBeInTheDocument();
    expect(screen.getByText("erika@example.com")).toBeInTheDocument();
  });

  it("changes the list of a connected account without signing in again", async () => {
    const api = setup({ target: testMsTodoTarget() });
    const user = userEvent.setup();
    await renderApp("/settings/task-export");

    await user.click(await screen.findByRole("button", { name: "Change" }));
    fireEvent.change(await screen.findByLabelText("List"), {
      target: { value: "AQMkADAw-work" },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.requests).toContainEqual({
        method: "PUT",
        path: "/api/todo-export/mstodo",
        body: { list_id: "AQMkADAw-work", mode: "auto" },
      }),
    );
  });
});
