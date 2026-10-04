import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pageNavigation } from "@/api/auth";
import type { MfaStatus } from "@/api/mfa";
import type { ReauthOptions } from "@/api/reauth";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

beforeEach(async () => {
  await i18n.changeLanguage("en");
  // biome-ignore lint/suspicious/noDocumentCookie: test setup for the CSRF cookie
  document.cookie = "ollamail_csrf=csrf-test-token; path=/";
});

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

const status: MfaStatus = {
  available: true,
  enforced: false,
  passkeys_configured: true,
  totp: true,
  passkeys: [],
  recovery_codes_remaining: 10,
};

function options(methods: ReauthOptions["methods"], extra: Partial<ReauthOptions> = {}) {
  return { methods, reauth_minutes: 10, valid_until: null, ...extra } satisfies ReauthOptions;
}

const REAUTH_REQUIRED = { type: "urn:ollamail:problem:reauth-required", reauth_minutes: 10 };

interface Call {
  route: string;
  body?: unknown;
}

/**
 * Signed-in backend whose sensitive endpoint `sensitive` answers 403 `reauth-required` until
 * `POST /api/auth/reauth` accepted the password "correct horse battery".
 */
function reauthBackend(sensitive: string, reauth: ReauthOptions, success: () => Response) {
  const calls: Call[] = [];
  let confirmed = false;
  const api = backend();
  const handler = async (request: Request) => {
    const route = `${request.method} ${new URL(request.url).pathname}`;
    const body = request.method === "GET" ? undefined : await request.text();
    calls.push({ route, body: body ? JSON.parse(body) : undefined });
    if (route === "GET /api/auth/mfa") return json(status);
    if (route === "GET /api/auth/reauth") return json(reauth);
    if (route === "POST /api/auth/reauth") {
      const sent = JSON.parse(body ?? "{}");
      if (sent.password === "correct horse battery" || sent.code === "123456") {
        confirmed = true;
        return json({
          authenticated_at: "2026-10-03T10:00:00Z",
          valid_until: "2026-10-03T10:10:00Z",
        });
      }
      return problem(400, { type: "urn:ollamail:problem:reauth-invalid" });
    }
    if (route === "POST /api/auth/logout") return new Response(null, { status: 204 });
    if (route === sensitive) return confirmed ? success() : problem(403, REAUTH_REQUIRED);
    return api(request);
  };
  return { handler, calls };
}

const count = (calls: Call[], route: string) => calls.filter((c) => c.route === route).length;

describe("confirmation before sensitive actions", () => {
  it("asks for the password and repeats the action", async () => {
    const { handler, calls } = reauthBackend(
      "DELETE /api/auth/mfa/totp",
      options(["password", "totp", "signin"]),
      () => new Response(null, { status: 204 }),
    );
    mockFetch(handler);
    const user = userEvent.setup();
    await renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    expect(sheet).toHaveTextContent(
      "“Remove authenticator app” needs a recent confirmation. It stays valid for 10 minutes.",
    );
    await user.type(within(sheet).getByLabelText("Password"), "wrong");
    await user.click(within(sheet).getByRole("button", { name: "Confirm" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "The password is not correct.",
    );

    await user.clear(within(sheet).getByLabelText("Password"));
    await user.type(within(sheet).getByLabelText("Password"), "correct horse battery");
    await user.click(within(sheet).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("Authenticator app removed.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Confirm it is you" })).not.toBeInTheDocument(),
    );
    expect(count(calls, "DELETE /api/auth/mfa/totp")).toBe(2);
    expect(calls.filter((c) => c.route === "POST /api/auth/reauth").map((c) => c.body)).toEqual([
      { method: "password", password: "wrong" },
      { method: "password", password: "correct horse battery" },
    ]);
  });

  it("confirms with the authenticator app instead", async () => {
    const { handler, calls } = reauthBackend(
      "POST /api/auth/mfa/recovery-codes",
      options(["password", "totp", "signin"]),
      () => json({ codes: ["abcde-fghjk"] }),
    );
    mockFetch(handler);
    const user = userEvent.setup();
    await renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "New codes" }));
    const panel = await screen.findByRole("dialog", { name: "Recovery codes" });
    await user.click(within(panel).getByRole("button", { name: "Create new codes" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(sheet).getByRole("button", { name: "Use your authenticator app" }));
    await user.type(within(sheet).getByLabelText("Code from your authenticator app"), "123456");
    await user.click(within(sheet).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByRole("list", { name: "Your recovery codes" })).toBeInTheDocument();
    expect(calls.find((c) => c.route === "POST /api/auth/reauth")?.body).toEqual({
      method: "totp",
      code: "123456",
    });
  });

  it("cancelling leaves everything as it was, without an error", async () => {
    const { handler, calls } = reauthBackend(
      "DELETE /api/auth/mfa/totp",
      options(["password", "signin"]),
      () => new Response(null, { status: 204 }),
    );
    mockFetch(handler);
    const user = userEvent.setup();
    await renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.click(within(sheet).getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Confirm it is you" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(count(calls, "DELETE /api/auth/mfa/totp")).toBe(1);
    expect(screen.getByRole("button", { name: "Remove" })).toBeEnabled();
  });

  it("sends SSO accounts back to their provider", async () => {
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    const { handler } = reauthBackend(
      "POST /api/privacy/exports",
      options(["sso", "signin"], {
        provider_display_name: "Contoso",
        login_path: "/auth/oidc/contoso/login",
      }),
      () => json({}),
    );
    mockFetch(handler);
    const user = userEvent.setup();
    // The way back after signing in comes from the address bar (the test router keeps it).
    window.history.replaceState(null, "", "/settings");
    await renderApp("/settings");

    await user.click(await screen.findByRole("button", { name: "Request export" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    expect(sheet).toHaveTextContent("You signed in with Contoso.");
    await user.click(within(sheet).getByRole("button", { name: "Continue with Contoso" }));

    expect(assign).toHaveBeenCalledWith("/api/auth/oidc/contoso/login?return_to=%2Fsettings");
  });

  it("offers signing in again when nothing else is possible", async () => {
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    const { handler, calls } = reauthBackend("POST /api/privacy/exports", options(["signin"]), () =>
      json({}),
    );
    mockFetch(handler);
    const user = userEvent.setup();
    // The way back after signing in comes from the address bar (the test router keeps it).
    window.history.replaceState(null, "", "/settings");
    await renderApp("/settings");

    await user.click(await screen.findByRole("button", { name: "Request export" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    expect(within(sheet).queryByRole("navigation")).not.toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Sign in again" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?redirect=%2Fsettings"));
    expect(count(calls, "POST /api/auth/logout")).toBe(1);
  });
});

describe("invitation with two-factor authentication", () => {
  it("continues with the enforced set-up after the password", async () => {
    window.history.replaceState(null, "", "/invite#token-123");
    const api = backend({ user: null });
    mockFetch((request) => {
      const { pathname } = new URL(request.url);
      if (pathname === "/api/auth/invitations/lookup") {
        return json({
          email: testUser.email,
          display_name: testUser.display_name,
          expires_at: "2026-10-09T08:00:00Z",
        });
      }
      if (pathname === "/api/auth/invitations/accept") {
        return json(
          {
            status: "mfa_enrollment_required",
            methods: ["totp"],
            expires_at: "2026-10-03T10:05:00Z",
          },
          { status: 202 },
        );
      }
      if (pathname === "/api/auth/mfa/totp/setup") {
        return json({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x", qr_svg: "<svg/>" });
      }
      return api(request);
    });
    const user = userEvent.setup();
    await renderApp("/invite");

    await user.type(await screen.findByLabelText("Password"), "correct horse battery");
    await user.type(screen.getByLabelText("Repeat password"), "correct horse battery");
    await user.click(screen.getByRole("button", { name: "Set password and sign in" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Set up two-factor authentication" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe("");
  });
});
