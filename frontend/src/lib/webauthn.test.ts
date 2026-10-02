import { describe, expect, it } from "vitest";

import { base64urlToBuffer, bufferToBase64url, creationOptions, requestOptions } from "./webauthn";

describe("webauthn JSON conversion", () => {
  it("round-trips base64url", () => {
    const bytes = new Uint8Array([0, 1, 250, 251, 252, 253, 254, 255]);
    const encoded = bufferToBase64url(bytes.buffer);

    expect(encoded).toBe("AAH6-_z9_v8");
    expect(new Uint8Array(base64urlToBuffer(encoded))).toEqual(bytes);
  });

  it("decodes challenge, user ID and credential IDs of the server options", () => {
    const created = creationOptions({
      challenge: "AAE",
      rp: { id: "mail.example.org", name: "ollamail" },
      user: { id: "AQI", name: "erika@example.org", displayName: "Erika" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      excludeCredentials: [{ id: "AwQ", type: "public-key", transports: ["internal"] }],
    });
    const requested = requestOptions({
      challenge: "BQY",
      rpId: "mail.example.org",
      allowCredentials: [],
    });

    expect(new Uint8Array(created.challenge as ArrayBuffer)).toEqual(new Uint8Array([0, 1]));
    expect(new Uint8Array(created.user.id as ArrayBuffer)).toEqual(new Uint8Array([1, 2]));
    expect(new Uint8Array(created.excludeCredentials?.[0]?.id as ArrayBuffer)).toEqual(
      new Uint8Array([3, 4]),
    );
    expect(created.rp.id).toBe("mail.example.org");
    expect(new Uint8Array(requested.challenge as ArrayBuffer)).toEqual(new Uint8Array([5, 6]));
    expect(requested.allowCredentials).toEqual([]);
  });
});
