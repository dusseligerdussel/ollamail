import type { Page } from "@playwright/test";

export interface MockApi {
  initialized?: boolean;
  /** Role of the signed-in user, `null` without a session. */
  role?: "admin" | "user" | null;
}

/**
 * Answers `/api` requests in the browser, for specs that run without a backend: a set-up
 * instance with a signed-in admin by default. Unknown endpoints return 404.
 */
export async function mockApi(page: Page, { initialized = true, role = "admin" }: MockApi = {}) {
  const user = role && {
    id: "00000000-0000-4000-8000-000000000001",
    email: "admin@example.org",
    display_name: "Test Admin",
    role,
    language: "en",
    timezone: "UTC",
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
    last_login_at: null,
  };
  const routes: Record<string, unknown> = {
    "GET /api/healthz": { status: "ok" },
    "GET /api/setup/status": { initialized },
    "GET /api/auth/providers": { local_login: true, local_registration: false, providers: [] },
    "GET /api/auth/me": user,
    "GET /api/auth/sessions": user && [
      {
        id: "00000000-0000-4000-8000-0000000000a1",
        provider: "local",
        created_at: "2026-01-01T08:00:00Z",
        last_seen_at: "2026-01-02T09:30:00Z",
        expires_at: "2026-01-15T08:00:00Z",
        user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0 Safari/537.36",
        current: true,
      },
    ],
    "GET /api/privacy/account": user && { self_delete_enabled: true, export_expiry_hours: 24 },
    "GET /api/privacy/exports": user && [],
    "GET /api/auth/mfa": user && {
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
    },
  };
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => {
      const request = route.request();
      const key = `${request.method()} ${new URL(request.url()).pathname}`;
      const body = routes[key];
      if (body) return route.fulfill({ json: body });
      const status = key in routes ? 401 : 404;
      return route.fulfill({
        status,
        contentType: "application/problem+json",
        json: { type: "about:blank", title: `HTTP ${status}`, status },
      });
    },
  );
}
