import { queryOptions } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

export type ScimSettings = Schemas["ScimSettingsRead"];
export type ScimSettingsUpdate = Schemas["ScimSettingsUpdate"];
export type ScimToken = Schemas["ScimTokenRead"];
export type ScimTokenIssued = Schemas["ScimTokenIssued"];

/** Provider key of the groups SCIM provisions (role mapping, shared mailbox groups). */
export const SCIM_PROVIDER = "scim";

export const scimQueryKey = ["admin", "scim"] as const;

export const scimSettingsQueryOptions = queryOptions({
  queryKey: scimQueryKey,
  queryFn: ({ signal }) => unwrap(api.GET("/admin/scim", { signal })),
  meta: { errorToast: false },
});

export function updateScimSettings(body: ScimSettingsUpdate) {
  return unwrap(api.PATCH("/admin/scim", { body }));
}

export function createScimToken(name: string, expiresInDays: number | null) {
  return unwrap(api.POST("/admin/scim/tokens", { body: { name, expires_in_days: expiresInDays } }));
}

export function revokeScimToken(tokenId: string) {
  return unwrap(
    api.DELETE("/admin/scim/tokens/{token_id}", { params: { path: { token_id: tokenId } } }),
  );
}
