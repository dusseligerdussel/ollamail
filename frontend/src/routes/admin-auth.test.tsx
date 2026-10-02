import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type {
  AdminUser,
  AuthSettings,
  LdapDirectory,
  OidcProvider,
  RoleMapping,
} from "@/api/admin-auth";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testAdmin, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

const LOCKOUT = { type: "urn:ollamail:problem:admin-lockout" };

const settings: AuthSettings = {
  local_login_enabled: true,
  local_registration: false,
  provider_kinds: ["ldap", "oidc"],
  admin_access: { usable_admins: 1, own_providers: ["local"] },
};

const entra: OidcProvider = {
  name: "microsoft",
  provider: "oidc:microsoft",
  source: "db",
  display_name: "Microsoft",
  preset: "entra",
  issuer: "https://login.microsoftonline.com/11111111-1111-4111-8111-111111111111/v2.0",
  client_id: "client-1",
  has_client_secret: true,
  scopes: ["openid", "email", "profile"],
  enabled: false,
  auto_provision: true,
  link_by_email: false,
  allowed_domains: [],
  groups_claim: "groups",
  allowed_tenants: [],
  hosted_domains: [],
  redirect_uri: "https://mail.example.org/api/auth/oidc/microsoft/callback",
  created_at: "2026-10-01T08:00:00Z",
  updated_at: "2026-10-01T08:00:00Z",
};

const directory: LdapDirectory = {
  id: "0199a0b4-0000-7000-8000-00000000d001",
  name: "corp",
  provider: "ldap:corp",
  display_name: "Corporate directory",
  enabled: true,
  bind_password_set: true,
  created_at: "2026-10-01T08:00:00Z",
  updated_at: "2026-10-01T08:00:00Z",
  settings: {
    directory_type: "active_directory",
    server_urls: ["ldaps://dc1.example.org"],
    tls_mode: "ldaps",
    bind_dn: "CN=svc,DC=example,DC=org",
    user_base_dn: "DC=example,DC=org",
    user_filter: "(sAMAccountName={login})",
    subject_attribute: "objectGUID",
    email_attribute: "mail",
    display_name_attribute: "displayName",
    group_filter: "(objectClass=group)",
    group_member_attribute: "member",
    nested_groups: true,
    connect_timeout: 5,
    operation_timeout: 10,
  },
};

function adminUser(overrides: Partial<AdminUser>): AdminUser {
  return {
    ...testAdmin,
    providers: ["local"],
    invitation_pending: false,
    active_sessions: 1,
    ...overrides,
  };
}

const users: AdminUser[] = [
  adminUser({}),
  adminUser({
    ...testUser,
    providers: ["oidc:microsoft"],
    active_sessions: 0,
  }),
];

interface Call {
  method: string;
  path: string;
  body: unknown;
}

/** Admin API mock: `overrides["METHOD /path"]` replaces a response; records all calls. */
function mockAdminApi(overrides: Record<string, () => Response> = {}) {
  const calls: Call[] = [];
  const api = backend();
  const mapping: RoleMapping = { enabled: false, default_role: "user", rules: [] };
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    const text = request.method === "GET" ? "" : await request.text();
    calls.push({ method: request.method, path: pathname, body: text ? JSON.parse(text) : null });
    const override = overrides[route];
    if (override) return override();
    switch (route) {
      case "GET /api/admin/auth/settings":
        return json(settings);
      case "GET /api/admin/auth/oidc/providers":
        return json([entra]);
      case "GET /api/auth/ldap/directories":
        return json([directory]);
      case "GET /api/admin/auth/role-mapping":
        return json(mapping);
      case "PUT /api/admin/auth/role-mapping":
        return json({ ...mapping, ...JSON.parse(text) });
      case "GET /api/users":
        return json(users);
      default:
        return api(request);
    }
  });
  return calls;
}

/** Radix menus open on keyboard input in jsdom (pointer events are incomplete there). */
async function openMenu(user: ReturnType<typeof userEvent.setup>, name: string) {
  (await screen.findByRole("button", { name: `Actions for ${name}` })).focus();
  await user.keyboard("{Enter}");
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

describe("admin: sign-in methods", () => {
  it("lists local login and the configured providers", async () => {
    mockAdminApi();
    await renderApp("/admin/sign-in");

    const section = await screen.findByRole("region", { name: "Sign-in methods" });
    expect(within(section).getByText("Local accounts")).toBeInTheDocument();
    expect(within(section).getByText("Microsoft")).toBeInTheDocument();
    expect(within(section).getByText("Corporate directory")).toBeInTheDocument();
    expect(screen.getByText("1 administrator can currently sign in.")).toBeInTheDocument();
  });

  it("warns before disabling local login and shows the server's lockout refusal", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi({
      "PATCH /api/admin/auth/settings": () => problem(409, LOCKOUT),
    });
    await renderApp("/admin/sign-in");

    await user.click(await screen.findByRole("button", { name: "Disable" }));
    const dialog = await screen.findByRole("dialog", { name: "Disable local sign-in?" });
    expect(within(dialog).getByText(/you lock yourself out/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Disable local sign-in" }));

    expect(await within(dialog).findByText(/no administrator could sign in/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ local_login_enabled: false });
  });

  it("adds an Entra ID provider: saved disabled, redirect URI, test, enable", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi({
      "POST /api/admin/auth/oidc/providers": () => json(entra, { status: 201 }),
      "POST /api/admin/auth/oidc/providers/microsoft/test": () =>
        json({
          ok: true,
          issuer: entra.issuer,
          authorization_endpoint: "https://login.microsoftonline.com/x/oauth2/v2.0/authorize",
          token_endpoint: "https://login.microsoftonline.com/x/oauth2/v2.0/token",
          end_session_supported: true,
          signing_keys: 3,
        }),
      "PATCH /api/admin/auth/oidc/providers/microsoft": () => json({ ...entra, enabled: true }),
    });
    await renderApp("/admin/sign-in");

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const sheet = await screen.findByRole("dialog");
    await user.click(within(sheet).getByRole("button", { name: /Microsoft Entra ID/ }));
    await user.type(
      within(sheet).getByLabelText("Directory (tenant) ID"),
      "11111111-1111-4111-8111-111111111111",
    );
    await user.type(within(sheet).getByLabelText("Client ID"), "client-1");
    await user.type(within(sheet).getByLabelText("Client secret"), "secret");
    await user.click(within(sheet).getByRole("button", { name: "Save and continue" }));

    expect(await within(sheet).findByDisplayValue(entra.redirect_uri)).toBeInTheDocument();
    const created = calls.find((c) => c.method === "POST" && c.path.endsWith("/providers"));
    expect(created?.body).toMatchObject({
      name: "microsoft",
      display_name: "Microsoft",
      preset: "entra",
      issuer: entra.issuer,
      client_id: "client-1",
      client_secret: "secret",
      enabled: false,
    });

    await user.click(within(sheet).getByRole("button", { name: "Test connection" }));
    expect(await within(sheet).findByText(/3 signing keys loaded/)).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Enable now" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ enabled: true }),
    );
  });

  it("offers GitHub only once it is available", async () => {
    const user = userEvent.setup();
    mockAdminApi();
    await renderApp("/admin/sign-in");

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByRole("button", { name: /GitHub/ })).toBeDisabled();
    expect(within(sheet).getByRole("button", { name: /LDAP/ })).toBeEnabled();
  });
});

describe("admin: role mapping", () => {
  it("saves rules and the default role", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi();
    await renderApp("/admin/role-mapping");

    await user.click(await screen.findByRole("radio", { name: "On" }));
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    const rules = screen.getByRole("region", { name: "Rules" });
    // fireEvent: in jsdom (no layout) the resize handle of the shell takes pointer focus.
    fireEvent.change(within(rules).getByLabelText("Group"), { target: { value: "mail-admins" } });
    await user.selectOptions(within(rules).getByLabelText("Provider"), "Microsoft");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
        enabled: true,
        default_role: "user",
        rules: [{ group: "mail-admins", provider: "oidc:microsoft", role: "admin" }],
      }),
    );
  });
});

describe("admin: users", () => {
  it("lists users with their sign-in methods", async () => {
    mockAdminApi();
    await renderApp("/admin/users");

    const table = await screen.findByRole("table", { name: "Users" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("You")).toBeInTheDocument();
    expect(await within(rows[1] as HTMLElement).findByText("Microsoft")).toBeInTheDocument();
  });

  it("asks before the own admin role is removed and shows the lockout refusal", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi({
      [`PATCH /api/users/${testAdmin.id}`]: () => problem(409, LOCKOUT),
    });
    await renderApp("/admin/users");

    await openMenu(user, testAdmin.display_name);
    await user.click(await screen.findByRole("menuitem", { name: "Remove administrator role" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Remove your own administrator role?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));

    expect(await within(dialog).findByText(/First make another user/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ role: "user" });
  });

  it("changes other users without asking", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi({
      [`PATCH /api/users/${testUser.id}`]: () => json({ ...users[1], role: "admin" }),
    });
    await renderApp("/admin/users");

    await openMenu(user, testUser.display_name);
    await user.click(await screen.findByRole("menuitem", { name: "Make administrator" }));

    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ role: "admin" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("invites a user and shows the link once", async () => {
    const user = userEvent.setup();
    const calls = mockAdminApi({
      "POST /api/users/invitations": () =>
        json(
          {
            user: adminUser({ ...testUser, providers: [], invitation_pending: true }),
            invite_url: "https://mail.example.org/invite#token-123",
            expires_at: "2026-10-09T08:00:00Z",
          },
          { status: 201 },
        ),
    });
    await renderApp("/admin/users");

    await user.click(await screen.findByRole("button", { name: "Invite user" }));
    const sheet = await screen.findByRole("dialog", { name: "Invite user" });
    await user.type(within(sheet).getByLabelText("E-mail address"), "max@example.org");
    await user.type(within(sheet).getByLabelText("Name"), "Max Muster");
    await user.click(within(sheet).getByRole("button", { name: "Create invitation" }));

    expect(
      await within(sheet).findByDisplayValue("https://mail.example.org/invite#token-123"),
    ).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/users/invitations")?.body).toMatchObject({
      email: "max@example.org",
      display_name: "Max Muster",
      role: "user",
    });
  });
});

describe("invitation page", () => {
  it("sets the password and signs in", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/invite#token-123");
    const calls: Call[] = [];
    const api = backend({ user: null });
    mockFetch(async (request) => {
      const { pathname } = new URL(request.url);
      if (pathname.startsWith("/api/auth/invitations/")) {
        calls.push({ method: request.method, path: pathname, body: await request.json() });
        if (pathname.endsWith("/lookup")) {
          return json({
            email: "max@example.org",
            display_name: "Max",
            expires_at: "2026-10-09T08:00:00Z",
          });
        }
        return json({ ...testUser, email: "max@example.org" });
      }
      return api(request);
    });
    const { router } = await renderApp("/invite");

    expect(await screen.findByText(/invited to ollamail as max@example.org/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.type(screen.getByLabelText("Repeat password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Set password and sign in" }));
    expect(await screen.findByText("The passwords do not match.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Repeat password"));
    await user.type(screen.getByLabelText("Repeat password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Set password and sign in" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/inbox"));
    expect(calls.at(-1)).toEqual({
      method: "POST",
      path: "/api/auth/invitations/accept",
      body: { token: "token-123", password: "correct horse battery" },
    });
  });

  it("explains an invalid link", async () => {
    window.history.replaceState(null, "", "/invite#expired");
    const api = backend({ user: null });
    mockFetch((request) =>
      new URL(request.url).pathname === "/api/auth/invitations/lookup"
        ? problem(404)
        : api(request),
    );
    await renderApp("/invite");

    expect(await screen.findByText(/invalid or has expired/)).toBeInTheDocument();
  });
});
