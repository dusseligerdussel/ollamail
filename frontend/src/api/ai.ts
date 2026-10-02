import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AISettings = components["schemas"]["AISettingsRead"];
export type AISettingsUpdate = components["schemas"]["AISettingsUpdate"];
export type AIProvider = components["schemas"]["AIProviderRead"];
export type AIProviderCreate = components["schemas"]["AIProviderCreate"];
export type AIProviderUpdate = components["schemas"]["AIProviderUpdate"];
export type AIProviderTest = components["schemas"]["AIProviderTest"];
export type AIConnectionTest = components["schemas"]["AIConnectionTest"];
export type AIStatus = components["schemas"]["AIStatusRead"];
export type LLMTask = components["schemas"]["LLMTask"];
export type LLMProfile = components["schemas"]["ProfileRead"]["name"];
export type LLMProviderKind = AIProvider["kind"];

/** Display order of the tasks (same as the backend's `LLMTask`). */
export const llmTasks: LLMTask[] = [
  "triage",
  "todos",
  "digest",
  "rag_chat",
  "reply_draft",
  "embeddings",
];

export const aiSettingsQueryOptions = queryOptions({
  queryKey: ["ai", "settings"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/ai/settings", { signal })),
});

export const aiProvidersQueryOptions = queryOptions({
  queryKey: ["ai", "providers"],
  queryFn: ({ signal }) => unwrap(api.GET("/admin/ai/providers", { signal })),
});

/** Cloud providers that receive mail content; shown to every user. */
export const aiStatusQueryOptions = queryOptions({
  queryKey: ["ai", "status"],
  queryFn: ({ signal }) => unwrap(api.GET("/ai/status", { signal })),
  staleTime: 5 * 60_000,
  refetchOnWindowFocus: true,
  // The notice is secondary: no error UI, no retries.
  retry: false,
  meta: { errorToast: false },
});

/** Models a provider serves (from a connection test). */
export function aiProviderModelsQueryOptions(name: string) {
  return queryOptions({
    queryKey: ["ai", "providers", name, "models"],
    queryFn: () => testProvider(name),
    staleTime: 5 * 60_000,
    meta: { errorToast: false },
  });
}

export function testProvider(name: string) {
  return unwrap(api.POST("/admin/ai/providers/{name}/test", { params: { path: { name } } }));
}

export function testProviderSettings(body: AIProviderTest) {
  return unwrap(api.POST("/admin/ai/providers/test", { body }));
}

function useInvalidateAI() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: ["ai"] });
}

export function useUpdateAISettings() {
  const invalidate = useInvalidateAI();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AISettingsUpdate) => unwrap(api.PATCH("/admin/ai/settings", { body })),
    onSuccess: (settings) => {
      queryClient.setQueryData(aiSettingsQueryOptions.queryKey, settings);
      return invalidate();
    },
  });
}

export function useCreateProvider() {
  const invalidate = useInvalidateAI();
  return useMutation({
    mutationFn: (body: AIProviderCreate) => unwrap(api.POST("/admin/ai/providers", { body })),
    onSuccess: invalidate,
  });
}

export function useUpdateProvider() {
  const invalidate = useInvalidateAI();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: AIProviderUpdate }) =>
      unwrap(api.PATCH("/admin/ai/providers/{name}", { params: { path: { name } }, body })),
    onSuccess: invalidate,
  });
}

export function useDeleteProvider() {
  const invalidate = useInvalidateAI();
  return useMutation({
    mutationFn: (name: string) =>
      unwrap(api.DELETE("/admin/ai/providers/{name}", { params: { path: { name } } })),
    onSuccess: invalidate,
  });
}
