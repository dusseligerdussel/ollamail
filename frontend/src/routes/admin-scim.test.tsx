import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ScimSettings, ScimToken } from "@/api/scim";
import { backend, json, mockFetch, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

const TOKEN_ID = "0199b000-0000-7000-8000-0000000000bb";
const SECRET = "olm_scim_s3cr3t-value-shown-once";

const token: ScimToken = {
  id: TOKEN_ID,
  name: "Entra ID",
  hint: "olm_scim_ab12",
  created_at: "2026-10-01T08:00:00Z",
  expires_at: null,
  last_used_at: "2026-10-02T09:30:00Z",
};

function settings(overrides: Partial<ScimSettings> = {}): ScimSettings {
  return {
    enabled: false,
    endpoint_url: "https://mail.example.org/api/scim/v2",
    link_providers: [],
    tokens: [],
    stats: { users: 0, active_users: 0, groups: 0 },
    ...overrides,
  };
}

/** SCIM admin API; records the request bodies. */
function mockScimApi(initial: ScimSettings) {
  let current = initial;
  const requests: { route: string; body?: unknown }[] = [];
  const base = backend();
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    if (route === "GET /api/admin/scim") return json(current);
    if (route === "PATCH /api/admin/scim") {
      const body = (await request.json()) as Partial<ScimSettings>;
      requests.push({ route, body });
      current = { ...current, ...body };
      return json(current);
    }
    if (route === "POST /api/admin/scim/tokens") {
      const body = await request.json();
      requests.push({ route, body });
      const created = { ...token, name: "Okta", last_used_at: null };
      current = { ...current, tokens: [...current.tokens, created] };
      return json({ token: created, secret: SECRET }, { status: 201 });
    }
    if (route === `DELETE /api/admin/scim/tokens/${TOKEN_ID}`) {
      requests.push({ route });
      current = { ...current, tokens: [] };
      return new Response(null, { status: 204 });
    }
    if (route === "GET /api/admin/auth/oidc/providers") {
      return json([{ provider: "oidc:entra", display_name: "Microsoft" }]);
    }
    if (route.startsWith("GET /api/admin/auth/") || route === "GET /api/auth/ldap/directories") {
      return json([]);
    }
    return base(request);
  });
  return { requests };
}

describe("admin: SCIM provisioning", () => {
  it("shows the endpoint and switches provisioning on", async () => {
    const { requests } = mockScimApi(settings());
    await renderApp("/admin/scim");
    expect(await screen.findByDisplayValue("https://mail.example.org/api/scim/v2")).toBeVisible();
    expect(screen.getByText("Nothing provisioned yet.")).toBeInTheDocument();
    expect(screen.getByText(/No tokens yet/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "On" }));
    await waitFor(() =>
      expect(requests).toEqual([{ route: "PATCH /api/admin/scim", body: { enabled: true } }]),
    );
  });

  it("creates a token and shows its secret once", async () => {
    const { requests } = mockScimApi(settings({ enabled: true }));
    await renderApp("/admin/scim");
    const create = await screen.findByRole("button", { name: "Create token" });
    expect(create).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Okta" } });
    fireEvent.change(screen.getByLabelText("Valid for"), { target: { value: "365" } });
    await userEvent.click(create);

    expect(await screen.findByDisplayValue(SECRET)).toBeVisible();
    expect(screen.getByText(/It will not be shown again/)).toBeInTheDocument();
    expect(requests).toContainEqual({
      route: "POST /api/admin/scim/tokens",
      body: { name: "Okta", expires_in_days: 365 },
    });
    const list = await screen.findByRole("list", { name: "Tokens" });
    expect(within(list).getByText("Okta")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByDisplayValue(SECRET)).not.toBeInTheDocument();
  });

  it("revokes a token after confirmation", async () => {
    const { requests } = mockScimApi(
      settings({ enabled: true, tokens: [token], stats: { users: 3, active_users: 2, groups: 1 } }),
    );
    await renderApp("/admin/scim");
    expect(await screen.findByText("3 users provisioned (2 active), 1 groups")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Tokens" });
    expect(within(list).getByText("olm_scim_ab12…")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Revoke token Entra ID" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(requests).toContainEqual({ route: `DELETE /api/admin/scim/tokens/${TOKEN_ID}` }),
    );
    expect(await screen.findByText(/No tokens yet/)).toBeInTheDocument();
  });

  it("lets admins choose providers that link sign-ins", async () => {
    const { requests } = mockScimApi(settings({ enabled: true }));
    await renderApp("/admin/scim");
    const linking = await screen.findByRole("list", { name: "Sign-in of provisioned users" });
    await userEvent.click(within(linking).getByRole("checkbox", { name: "Microsoft" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests).toContainEqual({
        route: "PATCH /api/admin/scim",
        body: { link_providers: ["oidc:entra"] },
      }),
    );
  });

  it("is only for admins", async () => {
    mockFetch(backend({ user: testUser }));
    await renderApp("/admin/scim");
    expect(screen.queryByText("Endpoint URL")).not.toBeInTheDocument();
  });
});
