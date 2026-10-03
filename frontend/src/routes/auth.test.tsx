import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { pageNavigation, safeRedirect, type User } from "@/api/auth";
import { CSRF_HEADER } from "@/api/client";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testAdmin, testSession, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

beforeEach(async () => {
  await i18n.changeLanguage("en");
  // biome-ignore lint/suspicious/noDocumentCookie: test setup for the CSRF cookie
  document.cookie = "ollamail_csrf=csrf-test-token; path=/";
});

/** Requests the app sent, as "METHOD /path". */
function sent(fetchMock: ReturnType<typeof mockFetch>) {
  return fetchMock.mock.calls.map(([input]) => {
    const request = input as Request;
    return `${request.method} ${new URL(request.url).pathname}`;
  });
}

function lastRequest(fetchMock: ReturnType<typeof mockFetch>, route: string) {
  const call = fetchMock.mock.calls
    .map(([input]) => input as Request)
    .findLast((request) => `${request.method} ${new URL(request.url).pathname}` === route);
  if (!call) throw new Error(`no request ${route}`);
  return call;
}

describe("setup wizard", () => {
  it("redirects a fresh instance to the setup and creates the first admin", async () => {
    const api = backend({ initialized: false, user: null });
    const fetchMock = mockFetch((request) =>
      request.method === "POST" && new URL(request.url).pathname === "/api/setup"
        ? json(testAdmin, { status: 201 })
        : api(request),
    );
    const user = userEvent.setup();
    const { router } = await renderApp("/inbox");

    expect(router.state.location.pathname).toBe("/setup");
    expect(screen.getByRole("heading", { level: 1, name: "Set up ollamail" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Name"), "Test Admin");
    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.type(screen.getByLabelText("Setup code"), "SETUPCODE");
    await user.click(screen.getByRole("button", { name: "Create administrator" }));

    await screen.findByRole("heading", { level: 1, name: "Administrator created" });
    const request = lastRequest(fetchMock, "POST /api/setup");
    expect(request.headers.get(CSRF_HEADER)).toBe("csrf-test-token");
    expect(await request.clone().json()).toEqual({
      display_name: "Test Admin",
      email: "admin@example.org",
      password: "correct horse battery",
      setup_token: "SETUPCODE",
      language: "en",
      timezone: expect.any(String),
    });
    expect(screen.getByRole("link", { name: "Set up identity provider" })).toHaveAttribute(
      "href",
      "/admin",
    );

    await user.click(screen.getByRole("link", { name: "Continue to inbox" }));
    await screen.findByRole("heading", { level: 1, name: "Inbox" });
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
  });

  it("shows a wrong setup code at the field", async () => {
    const api = backend({ initialized: false, user: null });
    mockFetch((request) =>
      request.method === "POST" ? problem(403, { detail: "invalid" }) : api(request),
    );
    const user = userEvent.setup();
    await renderApp("/setup");

    await user.type(screen.getByLabelText("Name"), "Test Admin");
    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.type(screen.getByLabelText("Setup code"), "WRONG");
    await user.click(screen.getByRole("button", { name: "Create administrator" }));

    expect(await screen.findByText("The setup code is incorrect.")).toBeInTheDocument();
    expect(screen.getByLabelText("Setup code")).toHaveAttribute("aria-invalid", "true");
  });

  it("explains a CSRF rejection instead of blaming the setup code", async () => {
    const api = backend({ initialized: false, user: null });
    mockFetch((request) =>
      request.method === "POST"
        ? problem(403, { detail: "CSRF token missing or invalid.", error_code: "csrf_failed" })
        : api(request),
    );
    const user = userEvent.setup();
    await renderApp("/setup");

    await user.type(screen.getByLabelText("Name"), "Test Admin");
    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.type(screen.getByLabelText("Setup code"), "SETUPCODE");
    await user.click(screen.getByRole("button", { name: "Create administrator" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The browser did not send the security cookie");
    expect(alert).toHaveTextContent("OLLAMAIL_AUTH_COOKIE_SECURE=false");
    expect(screen.getByRole("link", { name: /HTTPS and test operation/ })).toHaveAttribute(
      "href",
      expect.stringContaining("docs/OPERATIONS.md#26-http-ohne-tls-testbetrieb"),
    );
    expect(screen.queryByText("The setup code is incorrect.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Setup code")).not.toHaveAttribute("aria-invalid");
  });

  it("warns before submitting when the page is not a secure context", async () => {
    vi.stubGlobal("isSecureContext", false);
    mockFetch(backend({ initialized: false, user: null }));
    await renderApp("/setup");

    expect(screen.getByRole("alert")).toHaveTextContent("Unencrypted connection (HTTP)");
  });

  it("shows no connection notice in a secure context", async () => {
    mockFetch(backend({ initialized: false, user: null }));
    await renderApp("/setup");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the minimum password length the server requires", async () => {
    const api = backend({ initialized: false, user: null });
    mockFetch((request) =>
      request.method === "POST"
        ? problem(422, { type: "urn:ollamail:problem:password-too-short", min_length: 16 })
        : api(request),
    );
    const user = userEvent.setup();
    await renderApp("/setup");

    await user.type(screen.getByLabelText("Name"), "Test Admin");
    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "too short");
    await user.type(screen.getByLabelText("Setup code"), "SETUPCODE");
    await user.click(screen.getByRole("button", { name: "Create administrator" }));

    expect(
      await screen.findByText("The password must have at least 16 characters."),
    ).toBeInTheDocument();
  });

  it("is not reachable once the instance is set up", async () => {
    const { router } = await renderApp("/setup");
    expect(router.state.location.pathname).toBe("/inbox");
  });
});

describe("login", () => {
  function signInBackend(user: User = testAdmin) {
    let session = false;
    const api = backend({ user: null });
    return (request: Request) => {
      const route = `${request.method} ${new URL(request.url).pathname}`;
      if (route === "POST /api/auth/login") {
        session = true;
        return json(user);
      }
      return session ? backend({ user })(request) : api(request);
    };
  }

  it("redirects to the login page and back to the requested page after sign-in", async () => {
    const fetchMock = mockFetch(signInBackend());
    const user = userEvent.setup();
    const { router } = await renderApp("/tasks?filter=open");

    expect(router.state.location.pathname).toBe("/login");
    expect(router.state.location.search).toEqual({ redirect: "/tasks?filter=open" });
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("heading", { level: 1, name: "Tasks" });
    expect(router.state.location.href).toBe("/tasks?filter=open");
    const request = lastRequest(fetchMock, "POST /api/auth/login");
    expect(request.headers.get(CSRF_HEADER)).toBe("csrf-test-token");
    expect(await request.clone().json()).toEqual({
      email: "admin@example.org",
      password: "correct horse battery",
    });
  });

  it("switches to the language stored in the profile", async () => {
    mockFetch(signInBackend({ ...testAdmin, language: "de" }));
    const user = userEvent.setup();
    await renderApp("/login");

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("heading", { level: 1, name: "Posteingang" });
  });

  it("shows wrong credentials inline without leaving the page", async () => {
    const api = backend({ user: null });
    mockFetch((request) => (request.method === "POST" ? problem(401) : api(request)));
    const user = userEvent.setup();
    const { router } = await renderApp("/login");

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "wrong password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "E-mail address or password is incorrect.",
    );
    expect(router.state.location.pathname).toBe("/login");
  });

  it("shows the lockout", async () => {
    const api = backend({ user: null });
    mockFetch((request) => (request.method === "POST" ? problem(429) : api(request)));
    const user = userEvent.setup();
    await renderApp("/login");

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "wrong password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many sign-in attempts.");
  });

  it("explains a CSRF rejection instead of a generic error", async () => {
    const api = backend({ user: null });
    mockFetch((request) =>
      request.method === "POST" ? problem(403, { error_code: "csrf_failed" }) : api(request),
    );
    const user = userEvent.setup();
    await renderApp("/login");

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The browser did not send the security cookie");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("keeps other 403 responses as they are", async () => {
    const api = backend({ user: null });
    mockFetch((request) => (request.method === "POST" ? problem(403) : api(request)));
    const user = userEvent.setup();
    await renderApp("/login");

    await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
    await user.type(screen.getByLabelText("Password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("security cookie");
  });

  it("warns on the login page when the page is not a secure context", async () => {
    vi.stubGlobal("isSecureContext", false);
    mockFetch(backend({ user: null }));
    await renderApp("/login");

    expect(screen.getByRole("alert")).toHaveTextContent("Unencrypted connection (HTTP)");
  });

  it("renders buttons for the configured external providers", async () => {
    mockFetch(
      backend({
        user: null,
        providers: {
          local_login: true,
          local_registration: false,
          passkey_login: false,
          providers: [
            {
              name: "oidc:entra",
              display_name: "Microsoft Entra ID",
              kind: "redirect",
              login_path: "/auth/oidc/entra/login",
            },
            {
              name: "github:github",
              display_name: "GitHub",
              kind: "redirect",
              login_path: "/auth/github/github/login",
            },
            { name: "ldap:corp", display_name: "Corporate directory", kind: "password" },
          ],
        },
      }),
    );
    await renderApp("/login?redirect=%2Fdigest");

    const list = await screen.findByRole("list", { name: "Other sign-in methods" });
    const links = within(list).getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveTextContent("Continue with Microsoft Entra ID");
    expect(links[0]).toHaveAttribute("href", "/api/auth/oidc/entra/login?return_to=%2Fdigest");
    expect(links[1]).toHaveTextContent("Continue with GitHub");
    expect(links[1]).toHaveAttribute("href", "/api/auth/github/github/login?return_to=%2Fdigest");
  });

  it("shows no providers section without external providers", async () => {
    mockFetch(backend({ user: null }));
    await renderApp("/login");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("list", { name: "Other sign-in methods" })).not.toBeInTheDocument();
  });

  it("forwards signed-in users", async () => {
    const { router } = await renderApp("/login?redirect=%2Fsearch");
    expect(router.state.location.pathname).toBe("/search");
  });
});

describe("guards", () => {
  it("shows a 403 page and no admin navigation to non-admins", async () => {
    mockFetch(backend({ user: testUser }));
    await renderApp("/admin");

    expect(screen.getByRole("heading", { level: 1, name: "No access" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("shows an error page with retry when the server cannot be reached", async () => {
    let up = false;
    const api = backend();
    mockFetch((request) => (up ? api(request) : problem(503)));
    const user = userEvent.setup();
    // Server errors are retried twice (with backoff) before the error page appears.
    await renderApp("/inbox", { timeout: 5000 });

    expect(
      screen.getByRole("heading", { level: 1, name: "ollamail could not be loaded" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("temporarily unavailable");

    up = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("heading", { level: 1, name: "Inbox" });
  });
});

describe("account settings", () => {
  function accountBackend() {
    let profile: User = { ...testAdmin };
    let sessions = [
      testSession,
      {
        ...testSession,
        id: "00000000-0000-4000-8000-0000000000a2",
        current: false,
        user_agent:
          "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
      },
    ];
    const handler = async (request: Request) => {
      const { pathname } = new URL(request.url);
      const route = `${request.method} ${pathname}`;
      if (route === "GET /api/auth/me") return json(profile);
      if (route === "PATCH /api/auth/me") {
        profile = { ...profile, ...(await request.json()) };
        return json(profile);
      }
      if (route === "GET /api/auth/sessions") return json(sessions);
      if (request.method === "DELETE" && pathname.startsWith("/api/auth/sessions/")) {
        sessions = sessions.filter((s) => !pathname.endsWith(s.id));
        return new Response(null, { status: 204 });
      }
      if (route === "POST /api/auth/logout") return new Response(null, { status: 204 });
      return backend()(request);
    };
    return { handler, profile: () => profile };
  }

  it("shows the account and changes the time zone", async () => {
    const account = accountBackend();
    const fetchMock = mockFetch(account.handler);
    const user = userEvent.setup();
    await renderApp("/settings");

    const section = screen.getByRole("region", { name: "Account" });
    expect(within(section).getByText("Test Admin")).toBeInTheDocument();
    expect(within(section).getByText(/admin@example\.org · Administrator/)).toBeInTheDocument();

    await user.selectOptions(within(section).getByLabelText("Time zone"), "Europe/Berlin");
    await waitFor(() => expect(account.profile().timezone).toBe("Europe/Berlin"));
    expect(lastRequest(fetchMock, "PATCH /api/auth/me").headers.get(CSRF_HEADER)).toBe(
      "csrf-test-token",
    );
    expect(within(section).getByLabelText("Time zone")).toHaveValue("Europe/Berlin");
  });

  it("stores the language in the profile", async () => {
    const account = accountBackend();
    mockFetch(account.handler);
    const user = userEvent.setup();
    await renderApp("/settings");

    await user.click(screen.getByRole("radio", { name: "Deutsch" }));
    await screen.findByRole("heading", { level: 1, name: "Einstellungen" });
    await waitFor(() => expect(account.profile().language).toBe("de"));
  });

  it("lists the sessions and signs out another device", async () => {
    mockFetch(accountBackend().handler);
    const user = userEvent.setup();
    await renderApp("/settings");

    const section = screen.getByRole("region", { name: "Active sessions" });
    const [current, other] = await within(section).findAllByRole("listitem");
    if (!current || !other) throw new Error("expected two sessions");
    expect(current).toHaveTextContent("Firefox on Linux");
    expect(current).toHaveTextContent("This device");
    expect(current).toHaveTextContent("Last active: Jan 2, 2026, 9:30 AM");
    expect(within(current).queryByRole("button")).not.toBeInTheDocument();
    expect(other).toHaveTextContent("Safari on iOS");

    await user.click(within(other).getByRole("button", { name: "Sign out Safari on iOS" }));
    await waitFor(() => expect(within(section).getAllByRole("listitem")).toHaveLength(1));
    expect(
      within(section).queryByRole("button", { name: "Sign out all other sessions" }),
    ).not.toBeInTheDocument();
  });

  it("signs out and reloads the login page", async () => {
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    const fetchMock = mockFetch(accountBackend().handler);
    const user = userEvent.setup();
    await renderApp("/settings");

    await user.click(
      within(screen.getByRole("region", { name: "Account" })).getByRole("button", {
        name: "Sign out",
      }),
    );
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
    expect(sent(fetchMock)).toContain("POST /api/auth/logout");
    expect(lastRequest(fetchMock, "POST /api/auth/logout").headers.get(CSRF_HEADER)).toBe(
      "csrf-test-token",
    );
    assign.mockRestore();
  });
});

describe("safeRedirect", () => {
  it.each([
    ["/tasks?filter=open#x", "/tasks?filter=open#x"],
    [undefined, "/inbox"],
    ["https://evil.example/", "/inbox"],
    ["//evil.example/", "/inbox"],
    ["/\\evil.example", "/inbox"],
    ["javascript:alert(1)", "/inbox"],
    ["/login?redirect=/inbox", "/inbox"],
    ["/setup", "/inbox"],
  ])("%s → %s", (target, expected) => {
    expect(safeRedirect(target)).toBe(expected);
  });
});
