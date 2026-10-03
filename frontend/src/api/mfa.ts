import { queryOptions } from "@tanstack/react-query";

import { createPasskey, getPasskey } from "@/lib/webauthn";
import { api, createApiClient, unwrap } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

export type MfaStatus = components["schemas"]["MfaStatus"];
export type MfaChallenge = components["schemas"]["MfaChallenge"];
export type MfaMethod = components["schemas"]["MfaChallenge"]["methods"][number];
export type MfaEnrolled = components["schemas"]["MfaEnrolled"];
export type Passkey = components["schemas"]["PasskeyRead"];
export type TotpSetup = components["schemas"]["TotpSetup"];

/**
 * During the login (no session yet) a 401 means "pending sign-in expired"; the login page
 * handles it instead of redirecting to itself.
 */
const loginApi = createApiClient({ onUnauthorized: () => {} });

export const mfaQueryKey = ["auth", "mfa"] as const;

export const mfaStatusQueryOptions = queryOptions({
  queryKey: mfaQueryKey,
  queryFn: ({ signal }) => unwrap(api.GET("/auth/mfa", { signal })),
  meta: { errorToast: false },
});

export const MFA_EXPIRED = "urn:ollamail:problem:mfa-expired";
export const MFA_REQUIRED = "urn:ollamail:problem:mfa-required";

/** `POST /auth/login` (and accepting an invitation) answered 202: a second step follows. */
export function isMfaChallenge(result: object): result is MfaChallenge {
  return "status" in result && "methods" in result;
}

export function isMfaExpired(error: unknown) {
  return isApiError(error) && error.problem?.type === MFA_EXPIRED;
}

/** Client for account endpoints that also serve the enforced set-up during the login. */
function clientFor(duringLogin: boolean) {
  return duringLogin ? loginApi : api;
}

export function setupTotp(duringLogin = false) {
  return unwrap(clientFor(duringLogin).POST("/auth/mfa/totp/setup"));
}

export function confirmTotp(code: string, duringLogin = false) {
  return unwrap(clientFor(duringLogin).POST("/auth/mfa/totp/confirm", { body: { code } }));
}

export function removeTotp() {
  return unwrap(api.DELETE("/auth/mfa/totp"));
}

/** Runs the whole browser ceremony: options, `navigator.credentials.create`, register. */
export async function addPasskey(name: string, duringLogin = false) {
  const client = clientFor(duringLogin);
  const options = await unwrap(client.POST("/auth/mfa/passkeys/options"));
  const credential = await createPasskey(options);
  return unwrap(client.POST("/auth/mfa/passkeys", { body: { name, credential } }));
}

export function removePasskey(passkeyId: string) {
  return unwrap(
    api.DELETE("/auth/mfa/passkeys/{passkey_id}", {
      params: { path: { passkey_id: passkeyId } },
    }),
  );
}

export function regenerateRecoveryCodes() {
  return unwrap(api.POST("/auth/mfa/recovery-codes"));
}

// -- Login steps ---------------------------------------------------------------------------

export function verifyCode(method: "totp" | "recovery", code: string) {
  return unwrap(loginApi.POST("/auth/mfa/verify", { body: { method, code } }));
}

export async function verifyPasskey() {
  const options = await unwrap(loginApi.POST("/auth/mfa/verify/passkey/options"));
  const credential = await getPasskey(options);
  return unwrap(loginApi.POST("/auth/mfa/verify/passkey", { body: { credential } }));
}

export function cancelMfa() {
  return unwrap(loginApi.POST("/auth/mfa/cancel"));
}

/** Passwordless sign-in with a discoverable passkey. */
export async function signInWithPasskey() {
  const options = await unwrap(loginApi.POST("/auth/passkey/options"));
  const credential = await getPasskey(options);
  return unwrap(loginApi.POST("/auth/passkey/login", { body: { credential } }));
}
