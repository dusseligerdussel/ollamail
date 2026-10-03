import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type ModelStatus = components["schemas"]["ModelStatusRead"];
export type ModelPull = components["schemas"]["ModelPullRead"];
export type SystemOverview = components["schemas"]["SystemOverviewRead"];
export type MailboxProcessing = components["schemas"]["MailboxProcessingRead"];

export const systemKeys = {
  models: ["system", "models"] as const,
  overview: ["system", "overview"] as const,
};

/** A queued or running download; the list is polled while one is active. */
export function isPullActive(pull: ModelPull | null | undefined) {
  return pull?.status === "queued" || pull?.status === "running";
}

/**
 * Model state per task (admins only). Asks every LLM endpoint, so it is cached for a few
 * minutes and polled only while a download runs.
 */
export const modelStatusQueryOptions = queryOptions({
  queryKey: systemKeys.models,
  queryFn: ({ signal }) => unwrap(api.GET("/admin/system/models", { signal })),
  staleTime: 5 * 60_000,
  refetchOnWindowFocus: true,
  refetchInterval: (query) =>
    query.state.data?.some((model) => isPullActive(model.pull)) ? 2_000 : false,
  // The shell notice is secondary: no error toast, no retries.
  retry: false,
  meta: { errorToast: false },
});

export const systemOverviewQueryOptions = queryOptions({
  queryKey: systemKeys.overview,
  queryFn: ({ signal }) => unwrap(api.GET("/admin/system/overview", { signal })),
});

export function usePullModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { endpoint: string; model: string }) =>
      unwrap(api.POST("/admin/system/models/pull", { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: systemKeys.models }),
  });
}

export function useRetryFailed() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (mailboxId: string) =>
      unwrap(
        api.POST("/admin/system/mailboxes/{mailbox_id}/retry-failed", {
          params: { path: { mailbox_id: mailboxId } },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: systemKeys.overview }),
  });
}

/** "Classify older mails too": triage and todos for mails outside the backfill window. */
export function useIncludeOlder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (mailboxId: string) =>
      unwrap(
        api.POST("/admin/system/mailboxes/{mailbox_id}/include-older", {
          params: { path: { mailbox_id: mailboxId } },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: systemKeys.overview }),
  });
}
