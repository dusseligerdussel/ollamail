import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { MfaChallenge, MfaStatus } from "@/api/mfa";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testAdmin } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

beforeEach(async () => {
  await i18n.changeLanguage("en");
  // biome-ignore lint/suspicious/noDocumentCookie: test setup for the CSRF cookie
  document.cookie = "ollamail_csrf=csrf-test-token; path=/";
});

const RECOVERY_CODES = Array.from(
  { length: 10 },
  (_, i) => `abcde-fgh${String(i).padStart(2, "2")}`,
);
const QR = "data:image/svg+xml;base64,PHN2Zy8+";

function challenge(status: MfaChallenge["status"], methods: MfaChallenge["methods"]): MfaChallenge {
  return { status, methods, expires_at: "2026-10-02T12:05:00Z" };
}

type Routes = Record<string, (request: Request) => Response | Promise<Response>>;

/** Signed out until a route answers with the user; `routes` override single endpoints. */
function loginBackend(routes: Routes) {
  let session = false;
  return async (request: Request) => {
    const route = `${request.method} ${new URL(request.url).pathname}`;
    const handler = routes[route];
    if (handler) {
      const response = await handler(request);
      if (response.ok && route !== "POST /api/auth/login") session = true;
      return response;
    }
    return backend({ user: session ? testAdmin : null })(request);
  };
}

async function enterPassword() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("E-mail address"), "admin@example.org");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return user;
}

describe("second factor at sign-in", () => {
  it("asks for the authenticator code and shows a wrong code", async () => {
    const codes: string[] = [];
    mockFetch(
      loginBackend({
        "POST /api/auth/login": () =>
          json(challenge("mfa_required", ["totp", "recovery"]), { status: 202 }),
        "POST /api/auth/mfa/verify": async (request) => {
          const body = await request.json();
          codes.push(body.code);
          return body.code === "123456"
            ? json(testAdmin)
            : problem(401, { type: "urn:ollamail:problem:mfa-invalid" });
        },
      }),
    );
    await renderApp("/login");

    const user = await enterPassword();
    await screen.findByRole("heading", { level: 1, name: "Two-factor authentication" });
    await user.type(screen.getByLabelText("Code from your authenticator app"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The code is not correct. Try again.",
    );
    await user.clear(screen.getByLabelText("Code from your authenticator app"));
    await user.type(screen.getByLabelText("Code from your authenticator app"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByRole("heading", { level: 1, name: "Inbox" });
    expect(codes).toEqual(["000000", "123456"]);
  });

  it("switches to a recovery code", async () => {
    let sent: unknown;
    mockFetch(
      loginBackend({
        "POST /api/auth/login": () =>
          json(challenge("mfa_required", ["totp", "recovery"]), { status: 202 }),
        "POST /api/auth/mfa/verify": async (request) => {
          sent = await request.json();
          return json(testAdmin);
        },
      }),
    );
    await renderApp("/login");

    const user = await enterPassword();
    await user.click(await screen.findByRole("button", { name: "Use a recovery code" }));
    await user.type(screen.getByLabelText("Recovery code"), "abcde-fghjk");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByRole("heading", { level: 1, name: "Inbox" });
    expect(sent).toEqual({ method: "recovery", code: "abcde-fghjk" });
  });

  it("goes back to the password when the pending sign-in expired", async () => {
    mockFetch(
      loginBackend({
        "POST /api/auth/login": () => json(challenge("mfa_required", ["totp"]), { status: 202 }),
        "POST /api/auth/mfa/verify": () =>
          problem(401, { type: "urn:ollamail:problem:mfa-expired" }),
      }),
    );
    await renderApp("/login");

    const user = await enterPassword();
    await user.type(await screen.findByLabelText("Code from your authenticator app"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByRole("heading", { level: 1, name: "Sign in to ollamail" });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The sign-in has expired. Enter your password again.",
    );
  });

  it("sets up an authenticator app when 2FA is enforced, then shows the recovery codes", async () => {
    let confirmed: unknown;
    mockFetch(
      loginBackend({
        "POST /api/auth/login": () =>
          json(challenge("mfa_enrollment_required", ["totp"]), { status: 202 }),
        "POST /api/auth/mfa/totp/setup": () =>
          json({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x", qr_svg: QR }),
        "POST /api/auth/mfa/totp/confirm": async (request) => {
          confirmed = await request.json();
          return json({ recovery_codes: RECOVERY_CODES, user: testAdmin });
        },
      }),
    );
    await renderApp("/login");

    const user = await enterPassword();
    await screen.findByRole("heading", {
      level: 1,
      name: "Set up two-factor authentication",
    });
    expect(
      await screen.findByRole("img", { name: "QR code for the authenticator app" }),
    ).toHaveAttribute("src", QR);
    expect(screen.getByRole("textbox", { name: "Key for manual entry" })).toHaveValue(
      "JBSWY3DPEHPK3PXP",
    );
    await user.type(screen.getByLabelText("Code"), "123456");
    await user.click(screen.getByRole("button", { name: "Activate" }));

    await screen.findByRole("heading", { level: 1, name: "Save your recovery codes" });
    const list = screen.getByRole("list", { name: "Your recovery codes" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(10);
    expect(confirmed).toEqual({ code: "123456" });
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByRole("heading", { level: 1, name: "Inbox" });
  });
});

const status: MfaStatus = {
  available: true,
  enforced: false,
  passkeys_configured: true,
  totp: true,
  passkeys: [
    {
      id: "00000000-0000-4000-8000-0000000000b1",
      name: "Work laptop",
      created_at: "2026-09-01T10:00:00Z",
      last_used_at: "2026-10-01T08:00:00Z",
      backed_up: true,
    },
  ],
  recovery_codes_remaining: 8,
};

describe("Account → Security", () => {
  it("lists the factors and explains why the last one stays", async () => {
    const api = backend();
    mockFetch((request) => {
      const route = `${request.method} ${new URL(request.url).pathname}`;
      if (route === "GET /api/auth/mfa") return json({ ...status, enforced: true, totp: false });
      if (route.startsWith("DELETE /api/auth/mfa/passkeys/"))
        return problem(409, { type: "urn:ollamail:problem:mfa-required" });
      return api(request);
    });
    const user = userEvent.setup();
    await renderApp("/settings/security");

    expect(
      await screen.findByText(
        "Your administrator requires two-factor authentication for this account.",
      ),
    ).toBeInTheDocument();
    const passkeys = screen.getByRole("region", { name: "Passkeys" });
    expect(within(passkeys).getByText("Work laptop")).toBeInTheDocument();
    expect(within(passkeys).getByText("Synced")).toBeInTheDocument();
    expect(screen.getByText(/8 codes left/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove passkey Work laptop" }));
    expect(
      await screen.findByText(
        "Two-factor authentication is required for your account. Add another method before removing this one.",
      ),
    ).toBeInTheDocument();
  });

  it("regenerates recovery codes and shows them once", async () => {
    const api = backend();
    mockFetch((request) => {
      const route = `${request.method} ${new URL(request.url).pathname}`;
      if (route === "GET /api/auth/mfa") return json(status);
      if (route === "POST /api/auth/mfa/recovery-codes") return json({ codes: RECOVERY_CODES });
      return api(request);
    });
    const user = userEvent.setup();
    await renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "New codes" }));
    const sheet = await screen.findByRole("dialog", { name: "Recovery codes" });
    await user.click(within(sheet).getByRole("button", { name: "Create new codes" }));

    const list = await screen.findByRole("list", { name: "Your recovery codes" });
    expect(within(list).getByText(RECOVERY_CODES[0] as string)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done, codes saved" }));
    await waitFor(() =>
      expect(screen.queryByRole("list", { name: "Your recovery codes" })).not.toBeInTheDocument(),
    );
  });

  it("explains that SSO accounts manage 2FA at their provider", async () => {
    const api = backend();
    mockFetch((request) =>
      new URL(request.url).pathname === "/api/auth/mfa"
        ? json({ ...status, available: false })
        : api(request),
    );
    await renderApp("/settings/security");

    expect(
      await screen.findByText(
        "You sign in through an identity provider. Two-factor authentication is managed there.",
      ),
    ).toBeInTheDocument();
  });
});
