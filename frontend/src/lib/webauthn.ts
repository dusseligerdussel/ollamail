/**
 * Browser side of WebAuthn: the server sends options as JSON with base64url strings
 * (py_webauthn `options_to_json_dict`); the browser API wants ArrayBuffers and returns
 * credentials that are converted back to JSON for the server.
 */

type Json = Record<string, unknown>;

export function base64urlToBuffer(value: string): ArrayBuffer {
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

/** Whether this browser can use passkeys at all. */
export function webauthnSupported(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential === "function";
}

interface Descriptor {
  id: string;
  type: string;
  transports?: string[];
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined;
  return (list as Descriptor[]).map((item) => ({
    ...item,
    type: "public-key",
    id: base64urlToBuffer(item.id),
    transports: item.transports as AuthenticatorTransport[] | undefined,
  }));
}

export function creationOptions(options: Json): PublicKeyCredentialCreationOptions {
  const user = options.user as { id: string; name: string; displayName: string };
  return {
    ...(options as unknown as PublicKeyCredentialCreationOptions),
    challenge: base64urlToBuffer(options.challenge as string),
    user: { ...user, id: base64urlToBuffer(user.id) },
    excludeCredentials: descriptors(options.excludeCredentials),
  };
}

export function requestOptions(options: Json): PublicKeyCredentialRequestOptions {
  return {
    ...(options as unknown as PublicKeyCredentialRequestOptions),
    challenge: base64urlToBuffer(options.challenge as string),
    allowCredentials: descriptors(options.allowCredentials),
  };
}

function credentialJson(credential: PublicKeyCredential): Json {
  const response = credential.response;
  const common = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults?.() ?? {},
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
  };
  if ("attestationObject" in response) {
    const attestation = response as AuthenticatorAttestationResponse;
    return {
      ...common,
      response: {
        clientDataJSON: bufferToBase64url(attestation.clientDataJSON),
        attestationObject: bufferToBase64url(attestation.attestationObject),
        transports: attestation.getTransports?.() ?? [],
      },
    };
  }
  const assertion = response as AuthenticatorAssertionResponse;
  return {
    ...common,
    response: {
      clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
      authenticatorData: bufferToBase64url(assertion.authenticatorData),
      signature: bufferToBase64url(assertion.signature),
      userHandle: assertion.userHandle ? bufferToBase64url(assertion.userHandle) : undefined,
    },
  };
}

/** `navigator.credentials.create` for server options; the result as JSON for the server. */
export async function createPasskey(options: Json): Promise<Json> {
  const credential = await navigator.credentials.create({ publicKey: creationOptions(options) });
  if (!credential) throw new DOMException("No credential", "NotAllowedError");
  return credentialJson(credential as PublicKeyCredential);
}

/** `navigator.credentials.get` for server options; the result as JSON for the server. */
export async function getPasskey(options: Json): Promise<Json> {
  const credential = await navigator.credentials.get({ publicKey: requestOptions(options) });
  if (!credential) throw new DOMException("No credential", "NotAllowedError");
  return credentialJson(credential as PublicKeyCredential);
}

/** The user closed the browser dialog or it timed out (no error message needed). */
export function isWebauthnAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "NotAllowedError";
}
