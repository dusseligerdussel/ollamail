import { queryOptions } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

export type AuthSettings = Schemas["AuthSettingsRead"];
export type OidcProvider = Schemas["OIDCProviderRead"];
export type OidcProviderCreate = Schemas["OIDCProviderCreate"];
export type OidcPreset = Schemas["OIDCPreset"];
export type OidcConnectionTest = Schemas["OIDCConnectionTest"];
export type LdapDirectory = Schemas["LdapDirectoryRead"];
export type LdapDirectoryCreate = Schemas["LdapDirectoryCreate"];
export type LdapDirectorySettings = Schemas["LdapDirectorySettings"];
export type LdapConnectionTest = Schemas["LdapConnectionTest"];
export type LdapUserLookup = Schemas["LdapUserLookup"];
export type RoleMapping = Schemas["RoleMappingRead"];
export type RoleMappingUpdate = Schemas["RoleMappingUpdate"];
export type RoleMappingTestResult = Schemas["RoleMappingTestResult"];
export type Role = Schemas["UserRole"];
export type AdminUser = Schemas["AdminUserRead"];
export type UserInvite = Schemas["UserInvite"];
export type InvitationIssued = Schemas["InvitationIssued"];

/** Problem type when a change would leave no admin who can sign in (server-side check). */
export const ADMIN_LOCKOUT = "urn:ollamail:problem:admin-lockout";

export function isAdminLockout(error: unknown) {
  return isApiError(error) && error.problem?.type === ADMIN_LOCKOUT;
}

export const roles: Role[] = ["user", "admin"];

// -- Sign-in settings and providers ----------------------------------------------------

export const authSettingsQueryOptions = queryOptions({
  queryKey: ["admin", "auth", "settings"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/auth/settings", { signal })),
  meta: { errorToast: false },
});

export const oidcProvidersQueryOptions = queryOptions({
  queryKey: ["admin", "auth", "oidc"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/auth/oidc/providers", { signal })),
  meta: { errorToast: false },
});

export const ldapDirectoriesQueryOptions = queryOptions({
  queryKey: ["admin", "auth", "ldap"],
  queryFn: ({ signal }) => unwrap(api.GET("/auth/ldap/directories", { signal })),
  meta: { errorToast: false },
});

/** Prefix of every query above, to refresh them together after a change. */
export const adminAuthQueryKey = ["admin", "auth"] as const;

export function updateAuthSettings(body: Schemas["AuthSettingsUpdate"]) {
  return unwrap(api.PATCH("/admin/auth/settings", { body }));
}

export function createOidcProvider(body: OidcProviderCreate) {
  return unwrap(api.POST("/admin/auth/oidc/providers", { body }));
}

export function updateOidcProvider(name: string, body: Schemas["OIDCProviderUpdate"]) {
  return unwrap(
    api.PATCH("/admin/auth/oidc/providers/{name}", { params: { path: { name } }, body }),
  );
}

export function deleteOidcProvider(name: string) {
  return unwrap(api.DELETE("/admin/auth/oidc/providers/{name}", { params: { path: { name } } }));
}

export function testOidcProvider(name: string) {
  return unwrap(api.POST("/admin/auth/oidc/providers/{name}/test", { params: { path: { name } } }));
}

export function createLdapDirectory(body: LdapDirectoryCreate) {
  return unwrap(api.POST("/auth/ldap/directories", { body }));
}

/** LDAP updates replace the whole configuration; this keeps everything not in `changes`. */
export function updateLdapDirectory(
  directory: LdapDirectory,
  changes: Partial<Schemas["LdapDirectoryUpdate"]>,
) {
  const body: Schemas["LdapDirectoryUpdate"] = {
    display_name: directory.display_name,
    enabled: directory.enabled,
    settings: directory.settings,
    ...changes,
  };
  return unwrap(
    api.PUT("/auth/ldap/directories/{name}", { params: { path: { name: directory.name } }, body }),
  );
}

export function deleteLdapDirectory(name: string) {
  return unwrap(api.DELETE("/auth/ldap/directories/{name}", { params: { path: { name } } }));
}

export function testLdapDirectory(name: string) {
  return unwrap(api.POST("/auth/ldap/directories/{name}/test", { params: { path: { name } } }));
}

export function lookupLdapUser(name: string, login: string) {
  return unwrap(
    api.POST("/auth/ldap/directories/{name}/test-user", {
      params: { path: { name } },
      body: { login },
    }),
  );
}

/** Defaults the backend applies per directory type (`app/auth/providers/ldap/settings.py`). */
export const ldapPresets = {
  active_directory: {
    user_filter:
      "(&(objectCategory=person)(objectClass=user)(|(sAMAccountName={login})(userPrincipalName={login})))",
    subject_attribute: "objectGUID",
    email_attribute: "mail",
    display_name_attribute: "displayName",
    group_filter: "(objectClass=group)",
    group_member_attribute: "member",
  },
  openldap: {
    user_filter: "(&(objectClass=inetOrgPerson)(uid={login}))",
    subject_attribute: "entryUUID",
    email_attribute: "mail",
    display_name_attribute: "cn",
    group_filter: "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames))",
    group_member_attribute: "member",
  },
} as const satisfies Record<Schemas["DirectoryType"], Partial<LdapDirectorySettings>>;

// -- Role mapping ------------------------------------------------------------------------

export const roleMappingQueryOptions = queryOptions({
  queryKey: ["admin", "auth", "role-mapping"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/auth/role-mapping", { signal })),
  meta: { errorToast: false },
});

export function saveRoleMapping(body: RoleMappingUpdate) {
  return unwrap(api.PUT("/admin/auth/role-mapping", { body }));
}

export function testRoleMapping(provider: string, groups: string[]) {
  return unwrap(api.POST("/admin/auth/role-mapping/test", { body: { provider, groups } }));
}

// -- Users -------------------------------------------------------------------------------

export const adminUsersQueryOptions = queryOptions({
  queryKey: ["admin", "users"],
  queryFn: ({ signal }) => unwrap(api.GET("/users", { signal })),
  meta: { errorToast: false },
});

export function updateUser(userId: string, body: Schemas["AdminUserUpdate"]) {
  return unwrap(api.PATCH("/users/{user_id}", { params: { path: { user_id: userId } }, body }));
}

export function revokeUserSessions(userId: string) {
  return unwrap(api.DELETE("/users/{user_id}/sessions", { params: { path: { user_id: userId } } }));
}

export function inviteUser(body: UserInvite) {
  return unwrap(api.POST("/users/invitations", { body }));
}

export function reissueInvitation(userId: string) {
  return unwrap(api.POST("/users/{user_id}/invitation", { params: { path: { user_id: userId } } }));
}

// -- Invitation (public) -----------------------------------------------------------------

export function lookupInvitation(token: string) {
  return unwrap(api.POST("/auth/invitations/lookup", { body: { token } }));
}

export function acceptInvitation(token: string, password: string) {
  return unwrap(api.POST("/auth/invitations/accept", { body: { token, password } }));
}

/** Provider key (`oidc:entra`, `ldap:corp`) → kind for labels. */
export function providerKind(key: string): "local" | "oidc" | "ldap" | "github" | "other" {
  if (key === "local") return "local";
  const prefix = key.split(":", 1)[0];
  if (prefix === "oidc" || prefix === "ldap" || prefix === "github") return prefix;
  return "other";
}
