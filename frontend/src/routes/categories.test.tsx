import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Category, OrganizationCategory } from "@/api/triage";
import { backend, json, mockFetch, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";
import { categoryId, testCategories, testOrganizationCategories } from "@/test/triage";

interface Captured {
  method: string;
  path: string;
  body: unknown;
}

function mockCategoriesApi({
  categories = testCategories(),
  organization = testOrganizationCategories(),
  user = undefined as typeof testUser | undefined,
}: {
  categories?: Category[];
  organization?: OrganizationCategory[];
  user?: typeof testUser;
} = {}) {
  const requests: Captured[] = [];
  const api = backend(user ? { user } : {});
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    const text = request.method === "GET" ? "" : await request.clone().text();
    const body = text ? JSON.parse(text) : undefined;
    if (request.method !== "GET") requests.push({ method: request.method, path: pathname, body });
    if (route === "GET /api/triage/categories") return json(categories);
    if (route === "GET /api/triage/organization/categories") return json(organization);
    if (route === "PUT /api/triage/categories/order") {
      const ids = (body as { category_ids: string[] }).category_ids;
      return json(ids.map((id) => categories.find((category) => category.id === id)));
    }
    const patch = /^PATCH \/api\/triage\/(organization\/)?categories\/([^/]+)$/.exec(route);
    if (patch?.[2]) {
      const list = patch[1] ? organization : categories;
      return json({ ...list.find((category) => category.id === patch[2]), ...body });
    }
    if (route === "POST /api/triage/categories") {
      return json(
        {
          ...body,
          id: categoryId(20),
          builtin_key: null,
          scope: "user",
          hidden: false,
          position: 20,
        },
        { status: 201 },
      );
    }
    if (route === "POST /api/triage/organization/categories") {
      return json({ ...body, id: categoryId(21), builtin_key: null }, { status: 201 });
    }
    if (request.method === "DELETE") return new Response(null, { status: 204 });
    return api(request);
  });
  return requests;
}

async function openActions(name: string) {
  // Radix menus open on pointer or key events; jsdom handles the keyboard path reliably.
  (await screen.findByRole("button", { name: `Actions for ${name}` })).focus();
  await userEvent.keyboard("{Enter}");
}

describe("settings: categories", () => {
  it("is linked from the settings", async () => {
    mockCategoriesApi();
    await renderApp("/settings");
    expect(screen.getByRole("link", { name: /Manage categories/ })).toHaveAttribute(
      "href",
      "/settings/categories",
    );
  });

  it("lists organization and own categories in the user's order", async () => {
    mockCategoriesApi();
    await renderApp("/settings/categories");
    const list = await screen.findByRole("list", { name: "Categories" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(6);
    expect(rows[0]).toHaveTextContent("ImportantOrganization");
    expect(rows[5]).toHaveTextContent("Project ApolloMails about the Apollo project");
    // Only own categories can be edited or deleted.
    expect(screen.queryByRole("button", { name: "Actions for Important" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for Project Apollo" })).toBeInTheDocument();
  });

  it("reorders and hides categories", async () => {
    const requests = mockCategoriesApi();
    await renderApp("/settings/categories");
    await userEvent.click(await screen.findByRole("button", { name: "Move Important down" }));
    const ids = testCategories().map((category) => category.id);
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "PUT",
        path: "/api/triage/categories/order",
        body: { category_ids: [ids[1], ids[0], ...ids.slice(2)] },
      }),
    );

    await userEvent.click(screen.getByRole("switch", { name: "Show Info" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "PATCH",
        path: `/api/triage/categories/${categoryId(4)}`,
        body: { hidden: true },
      }),
    );
  });

  it("keeps the last visible category visible", async () => {
    const categories = testCategories().map((category, index) => ({
      ...category,
      hidden: index > 0,
    }));
    mockCategoriesApi({ categories });
    await renderApp("/settings/categories");
    expect(await screen.findByRole("switch", { name: "Show Important" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Show Info" })).toBeEnabled();
  });

  it("adds, edits and deletes own categories", async () => {
    const requests = mockCategoriesApi();
    await renderApp("/settings/categories");

    await userEvent.click(await screen.findByRole("button", { name: "Add category" }));
    const sheet = await screen.findByRole("dialog", { name: "Add category" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    expect(within(sheet).getByText("Please enter a name.")).toBeInTheDocument();
    await userEvent.type(within(sheet).getByLabelText("Name"), "Invoices");
    await userEvent.type(within(sheet).getByLabelText("Description"), "Invoices from suppliers");
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "POST",
        path: "/api/triage/categories",
        body: { name: "Invoices", description: "Invoices from suppliers" },
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await openActions("Project Apollo");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const edit = await screen.findByRole("dialog", { name: "Edit category" });
    const name = within(edit).getByLabelText("Name");
    expect(name).toHaveValue("Project Apollo");
    await userEvent.clear(name);
    await userEvent.type(name, "Apollo");
    await userEvent.click(within(edit).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "PATCH",
        path: `/api/triage/categories/${categoryId(9)}`,
        body: { name: "Apollo", description: "Mails about the Apollo project" },
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await openActions("Project Apollo");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete category?" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "DELETE",
        path: `/api/triage/categories/${categoryId(9)}`,
        body: undefined,
      }),
    );
  });
});

describe("admin: organization categories", () => {
  it("is not shown to non-admins", async () => {
    mockCategoriesApi({ user: testUser });
    await renderApp("/admin/categories");
    expect(screen.getByRole("heading", { level: 1, name: "No access" })).toBeInTheDocument();
  });

  it("is linked from the admin page", async () => {
    mockCategoriesApi();
    await renderApp("/admin");
    expect(screen.getByRole("link", { name: /Organization categories/ })).toHaveAttribute(
      "href",
      "/admin/categories",
    );
  });

  it("adds categories and renumbers positions when moving", async () => {
    const requests = mockCategoriesApi();
    await renderApp("/admin/categories");
    await userEvent.click(await screen.findByRole("button", { name: "Move Info up" }));
    await waitFor(() =>
      expect(requests.filter((request) => request.method === "PATCH")).toEqual([
        {
          method: "PATCH",
          path: `/api/triage/organization/categories/${categoryId(4)}`,
          body: { position: 2 },
        },
        {
          method: "PATCH",
          path: `/api/triage/organization/categories/${categoryId(3)}`,
          body: { position: 3 },
        },
      ]),
    );

    await userEvent.click(screen.getByRole("button", { name: "Add category" }));
    const sheet = await screen.findByRole("dialog", { name: "Add category" });
    await userEvent.type(within(sheet).getByLabelText("Name"), "Contracts");
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "POST",
        path: "/api/triage/organization/categories",
        body: { name: "Contracts", description: "", position: 5 },
      }),
    );
  });

  it("keeps the translation of a default category unless it is renamed", async () => {
    const requests = mockCategoriesApi();
    await renderApp("/admin/categories");
    await openActions("Action required");
    await userEvent.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const sheet = await screen.findByRole("dialog", { name: "Edit category" });
    expect(
      within(sheet).getByText("Renamed default categories are no longer translated automatically."),
    ).toBeInTheDocument();
    await userEvent.type(within(sheet).getByLabelText("Description"), " Also approvals.");
    await userEvent.click(within(sheet).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        method: "PATCH",
        path: `/api/triage/organization/categories/${categoryId(2)}`,
        body: { description: "Description of action_required Also approvals." },
      }),
    );
  });
});
