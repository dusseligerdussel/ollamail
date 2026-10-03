import { queryOptions } from "@tanstack/react-query";

import { getPasskey } from "@/lib/webauthn";
import { api, unwrap } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

/**
 * Confirmation before sensitive actions (`app/auth/reauth.py`): removing a second factor,
 * new recovery codes, the data export and deleting the account answer 403 with this type
 * when the last sign-in or confirmation is too old.
 */
export const REAUTH_REQUIRED = "urn:ollamail:problem:reauth-required";

export type ReauthOptions = components["schemas"]["ReauthOptions"];
export type ReauthMethod = ReauthOptions["methods"][number];

export function isReauthRequired(error: unknown): boolean {
  return isApiError(error) && error.status === 403 && error.problem?.type === REAUTH_REQUIRED;
}

/** The user closed the confirmation sheet; the action was not carried out. */
export class ReauthCancelledError extends Error {
  constructor() {
    super("Confirmation cancelled");
    this.name = "ReauthCancelledError";
  }
}

export function isReauthCancelled(error: unknown): boolean {
  return error instanceof ReauthCancelledError;
}

export const reauthOptionsQueryOptions = queryOptions({
  queryKey: ["auth", "reauth"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/reauth", { signal })),
  staleTime: 0,
  meta: { errorToast: false },
});

export function confirmWithPassword(password: string) {
  return unwrap(api.POST("/auth/reauth", { body: { method: "password", password } }));
}

export function confirmWithCode(code: string) {
  return unwrap(api.POST("/auth/reauth", { body: { method: "totp", code } }));
}

/** Runs the browser ceremony: options, `navigator.credentials.get`, confirm. */
export async function confirmWithPasskey() {
  const options = await unwrap(api.POST("/auth/reauth/passkey/options"));
  const credential = await getPasskey(options);
  return unwrap(api.POST("/auth/reauth/passkey", { body: { credential } }));
}

/** The page to come back to after signing in again. */
export function currentPath() {
  const { pathname, search } = window.location;
  return `${pathname}${search}`;
}

/** Start URL of the provider's login (full page navigation, back to `returnTo`). */
export function providerReauthUrl(loginPath: string, returnTo: string) {
  return `/api${loginPath}?${new URLSearchParams({ return_to: returnTo })}`;
}
