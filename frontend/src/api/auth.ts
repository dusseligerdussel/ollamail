import { type QueryClient, queryOptions } from "@tanstack/react-query";

import { api, createApiClient, unwrap } from "./client";
import { isApiError } from "./errors";
import { forgetPushDevice } from "./notifications";
import type { components } from "./schema.gen";

export type User = components["schemas"]["UserRead"];
export type AuthSession = components["schemas"]["SessionRead"];
export type LinkNotice = components["schemas"]["LinkNoticeRead"];
export type SignInIdentity = components["schemas"]["IdentityRead"];
export type LinkBlock = components["schemas"]["LinkBlockRead"];
export type AuthProviders = components["schemas"]["AuthProviders"];
export type AuthProviderInfo = components["schemas"]["AuthProviderInfo"];

/**
 * Client for requests where 401 is an expected answer (`/auth/me` before sign-in, wrong
 * password): no redirect to the login page, the caller handles it.
 */
const authApi = createApiClient({ onUnauthorized: () => {} });

export const setupStatusQueryOptions = queryOptions({
  queryKey: ["setup", "status"],
  queryFn: ({ signal }) => unwrap(api.GET("/setup/status", { signal })),
  // Once initialised, an instance stays initialised.
  staleTime: ({ state }) => (state.data?.initialized ? Number.POSITIVE_INFINITY : 0),
  meta: { errorToast: false },
});

/** The signed-in user, or `null` without a valid session. */
export const currentUserQueryOptions = queryOptions({
  queryKey: ["auth", "me"],
  queryFn: async ({ signal }): Promise<User | null> => {
    try {
      return await unwrap(authApi.GET("/auth/me", { signal }));
    } catch (error) {
      if (isApiError(error) && error.isUnauthorized) return null;
      throw error;
    }
  },
  // Only changes through sign-in, sign-out (full reload) and `PATCH /auth/me`, which update the
  // cache. An expired session shows up as 401 on the next API call (login redirect).
  staleTime: Number.POSITIVE_INFINITY,
  meta: { errorToast: false },
});

export const authProvidersQueryOptions = queryOptions({
  queryKey: ["auth", "providers"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/providers", { signal })),
  meta: { errorToast: false },
});

export const sessionsQueryOptions = queryOptions({
  queryKey: ["auth", "sessions"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/sessions", { signal })),
  meta: { errorToast: false },
});

/** Sign-ins linked to the own account by e-mail address, not acknowledged yet (#208). */
export const linkNoticesQueryOptions = queryOptions({
  queryKey: ["auth", "link-notices"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/link-notices", { signal })),
  meta: { errorToast: false },
});

/** The own ways to sign in, with whether each can be unlinked (#216). */
export const identitiesQueryOptions = queryOptions({
  queryKey: ["auth", "identities"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/identities", { signal })),
  meta: { errorToast: false },
});

/** Providers blocked from linking to the own account by e-mail address again (#216). */
export const linkBlocksQueryOptions = queryOptions({
  queryKey: ["auth", "link-blocks"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/link-blocks", { signal })),
  meta: { errorToast: false },
});

export type SetupInput = components["schemas"]["SetupRequest"];
export type LoginInput = components["schemas"]["LoginRequest"];
export type ProfileUpdate = components["schemas"]["ProfileUpdate"];

export function createFirstAdmin(body: SetupInput) {
  return unwrap(authApi.POST("/setup", { body }));
}

export function login(body: LoginInput) {
  return unwrap(authApi.POST("/auth/login", { body }));
}

export function updateProfile(body: ProfileUpdate) {
  return unwrap(api.PATCH("/auth/me", { body }));
}

export function revokeSession(sessionId: string) {
  return unwrap(
    api.DELETE("/auth/sessions/{session_id}", { params: { path: { session_id: sessionId } } }),
  );
}

export function dismissLinkNotice(noticeId: string) {
  return unwrap(
    api.DELETE("/auth/link-notices/{notice_id}", { params: { path: { notice_id: noticeId } } }),
  );
}

/** Needs a recent confirmation (`useReauth`). */
export function unlinkIdentity(identityId: string) {
  return unwrap(
    api.DELETE("/auth/identities/{identity_id}", {
      params: { path: { identity_id: identityId } },
    }),
  );
}

/** Needs a recent confirmation (`useReauth`). */
export function liftLinkBlock(blockId: string) {
  return unwrap(
    api.DELETE("/auth/link-blocks/{block_id}", { params: { path: { block_id: blockId } } }),
  );
}

export function revokeOtherSessions() {
  return unwrap(api.DELETE("/auth/sessions", { params: { query: { include_current: false } } }));
}

/** Store the user returned by setup or sign-in, so the route guards see the new session. */
export function setSignedIn(queryClient: QueryClient, user: User) {
  queryClient.setQueryData(setupStatusQueryOptions.queryKey, { initialized: true });
  queryClient.setQueryData(currentUserQueryOptions.queryKey, user);
}

/**
 * Ends the session and loads the login page from scratch, so that no data of the signed-out
 * user stays in memory (query cache, router state).
 */
export function logout() {
  return logoutTo("/login");
}

/** Like `logout`, but continues at `url` (e.g. the login page with a `redirect`). */
export async function logoutTo(url: string) {
  // While still signed in: this browser no longer receives the user's notifications (#181).
  await forgetPushDevice();
  await unwrap(authApi.POST("/auth/logout"));
  pageNavigation.assign(url);
}

/** Full page navigation (replaceable in tests; jsdom does not navigate). */
export const pageNavigation = {
  assign(url: string) {
    window.location.assign(url);
  },
};

/**
 * Where to go after sign-in: only paths on this origin (no open redirect via `?redirect=`),
 * never back to the login or setup page.
 */
export function safeRedirect(target: unknown, fallback = "/inbox"): string {
  if (typeof target !== "string" || !target.startsWith("/") || target.startsWith("//")) {
    return fallback;
  }
  if (target.includes("\\")) return fallback;
  const { pathname } = new URL(target, window.location.origin);
  if (pathname === "/login" || pathname === "/setup") return fallback;
  return target;
}
