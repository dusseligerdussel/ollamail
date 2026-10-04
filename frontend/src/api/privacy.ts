import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";
import { useReauth } from "@/components/auth/reauth";
import { API_BASE_PATH, api, unwrap } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

export type DataExport = components["schemas"]["DataExportRead"];
export type AccountPrivacy = components["schemas"]["AccountPrivacyRead"];
export type RetentionSettings = components["schemas"]["RetentionSettingsRead"];
export type RetentionValues = components["schemas"]["RetentionValues"];
export type RetentionUpdate = components["schemas"]["RetentionSettingsUpdate"];
export type RetentionField = keyof RetentionValues;
export type UserDeletionResult = components["schemas"]["UserDeletionResult"];

const LAST_ADMIN = "urn:ollamail:problem:last-admin";

/** 409: the last active administrator cannot be deleted. */
export function isLastAdmin(error: unknown) {
  return isApiError(error) && error.problem?.type === LAST_ADMIN;
}

/** Display order of the retention periods. */
export const retentionFields: RetentionField[] = [
  "mail_days",
  "attachment_days",
  "search_index_days",
  "rag_history_days",
  "digest_days",
  "audit_days",
];

export const accountPrivacyQueryOptions = queryOptions({
  queryKey: ["privacy", "account"],
  queryFn: ({ signal }) => unwrap(api.GET("/privacy/account", { signal })),
  staleTime: 5 * 60_000,
});

/** Own exports; refreshed by the `privacy.export` server event. */
export const exportsQueryOptions = queryOptions({
  queryKey: ["privacy", "exports"],
  queryFn: ({ signal }) => unwrap(api.GET("/privacy/exports", { signal })),
});

export const retentionQueryOptions = queryOptions({
  queryKey: ["privacy", "retention"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/privacy/retention", { signal })),
});

/** Download link of a finished export (same origin, authenticated by the session cookie). */
export function exportDownloadUrl(id: string) {
  return `${API_BASE_PATH}/privacy/exports/${encodeURIComponent(id)}/download`;
}

export function useRequestExport() {
  const queryClient = useQueryClient();
  const withReauth = useReauth();
  return useMutation({
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: () => withReauth(() => unwrap(api.POST("/privacy/exports"))),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: exportsQueryOptions.queryKey }),
  });
}

export function useDeleteExport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE("/privacy/exports/{export_id}", { params: { path: { export_id: id } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: exportsQueryOptions.queryKey }),
  });
}

export function useDeleteAccount() {
  const withReauth = useReauth();
  return useMutation({
    // Shown inline in the confirmation dialog.
    meta: { errorToast: false },
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: (confirmEmail: string) =>
      withReauth(() =>
        unwrap(api.DELETE("/privacy/account", { body: { confirm_email: confirmEmail } })),
      ),
  });
}

/** Admin: delete another user (or the own account) with all their data. */
export function useDeleteUser() {
  const withReauth = useReauth();
  return useMutation({
    // Shown inline in the confirmation dialog.
    meta: { errorToast: false },
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: (userId: string) =>
      withReauth(() =>
        unwrap(
          api.DELETE("/admin/privacy/users/{user_id}", { params: { path: { user_id: userId } } }),
        ),
      ),
  });
}

export function useUpdateRetention() {
  const queryClient = useQueryClient();
  const withReauth = useReauth();
  return useMutation({
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: (body: RetentionUpdate) =>
      withReauth(() => unwrap(api.PATCH("/admin/privacy/retention", { body }))),
    onSuccess: (settings) => queryClient.setQueryData(retentionQueryOptions.queryKey, settings),
  });
}
