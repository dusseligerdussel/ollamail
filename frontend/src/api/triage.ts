import {
  infiniteQueryOptions,
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type Category = components["schemas"]["CategoryRead"];
export type CategoryCreate = components["schemas"]["CategoryCreate"];
export type CategoryUpdate = components["schemas"]["CategoryUpdate"];
export type OrganizationCategory = components["schemas"]["OrganizationCategoryRead"];
export type OrganizationCategoryCreate = components["schemas"]["OrganizationCategoryCreate"];
export type OrganizationCategoryUpdate = components["schemas"]["OrganizationCategoryUpdate"];
export type Triage = components["schemas"]["TriageRead"];
export type TriagedMessage = components["schemas"]["TriagedMessage"];
export type TriagedMessagePage = components["schemas"]["TriagedMessagePage"];

/** Built-in categories; the UI shows a translated name for these (`triage.builtin.<key>`). */
export const builtinCategoryKeys = [
  "important",
  "action_required",
  "waiting_for",
  "info",
  "newsletter",
  "notification",
  "spam",
] as const;

/**
 * Query keys. Triage results and the inbox ordered by category start with `["message"]`, so
 * `message.*` server events (incl. `message.triaged`) refresh them; categories are user
 * settings and refreshed by the mutations below.
 */
export const triageKeys = {
  categories: ["triage", "categories"] as const,
  organizationCategories: ["triage", "organization", "categories"] as const,
  all: ["message", "triage"] as const,
  result: (messageId: string) => ["message", "triage", "result", messageId] as const,
  inbox: (filters: TriageInboxFilters) => ["message", "triage", "inbox", filters] as const,
};

export const categoriesQueryOptions = queryOptions({
  queryKey: triageKeys.categories,
  queryFn: ({ signal }) => unwrap(api.GET("/triage/categories", { signal })),
  staleTime: 5 * 60_000,
});

export const organizationCategoriesQueryOptions = queryOptions({
  queryKey: triageKeys.organizationCategories,
  queryFn: ({ signal }) => unwrap(api.GET("/triage/organization/categories", { signal })),
});

// -- triage of single messages, loaded in batches -------------------------------------------

/** Most IDs per request (the API's limit). */
export const TRIAGE_BATCH_SIZE = 200;

interface Pending {
  resolve: (triage: Triage | null) => void;
  reject: (error: unknown) => void;
}

/**
 * Collects the message IDs requested in the same tick (e.g. all visible list rows) and loads
 * them with one `GET /triage/messages?ids=…`. Resolves `null` for messages not triaged yet.
 */
export function createTriageLoader(fetchMany: (ids: string[]) => Promise<Triage[]> = fetchTriages) {
  let queue = new Map<string, Pending[]>();
  let scheduled = false;

  async function flush() {
    scheduled = false;
    const batch = queue;
    queue = new Map();
    const ids = [...batch.keys()];
    for (let start = 0; start < ids.length; start += TRIAGE_BATCH_SIZE) {
      const chunk = ids.slice(start, start + TRIAGE_BATCH_SIZE);
      try {
        const results = new Map((await fetchMany(chunk)).map((r) => [r.message_id, r]));
        for (const id of chunk) {
          for (const pending of batch.get(id) ?? []) pending.resolve(results.get(id) ?? null);
        }
      } catch (error) {
        for (const id of chunk) for (const pending of batch.get(id) ?? []) pending.reject(error);
      }
    }
  }

  return (messageId: string) =>
    new Promise<Triage | null>((resolve, reject) => {
      queue.set(messageId, [...(queue.get(messageId) ?? []), { resolve, reject }]);
      if (!scheduled) {
        scheduled = true;
        setTimeout(() => void flush(), 0);
      }
    });
}

function fetchTriages(ids: string[]) {
  return unwrap(api.GET("/triage/messages", { params: { query: { ids } } }));
}

const loadTriage = createTriageLoader();

export function triageQueryOptions(messageId: string) {
  return queryOptions({
    queryKey: triageKeys.result(messageId),
    queryFn: () => loadTriage(messageId),
    // Rows mount and unmount while scrolling; `message.triaged` events keep the data fresh.
    staleTime: 5 * 60_000,
    // A missing label is not worth an error message in every row.
    meta: { errorToast: false },
  });
}

// -- inbox ordered by category ---------------------------------------------------------------

/**
 * `category`: `"all"` lists all inbox messages grouped by category, a category ID or `"none"`
 * only that group.
 */
export interface TriageInboxFilters {
  mailbox?: string;
  unread?: boolean;
  category: string;
}

export const TRIAGE_PAGE_SIZE = 100;

export function triageInboxQueryOptions(filters: TriageInboxFilters) {
  return infiniteQueryOptions({
    queryKey: triageKeys.inbox(filters),
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/triage/inbox/messages", {
          params: {
            query: {
              mailbox_id: filters.mailbox,
              unread: filters.unread,
              category: filters.category === "all" ? undefined : filters.category,
              cursor: pageParam,
              limit: TRIAGE_PAGE_SIZE,
            },
          },
          signal,
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}

// -- mutations -------------------------------------------------------------------------------

export function correctTriage(messageId: string, categoryId: string, priority: number) {
  return unwrap(
    api.PUT("/triage/messages/{message_id}", {
      params: { path: { message_id: messageId } },
      body: { category_id: categoryId, priority },
    }),
  );
}

/** Correct the category of a message; keeps its priority (normal if it had none). */
export function useCorrectTriage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ messageId, categoryId, priority }: CorrectTriageInput) =>
      correctTriage(messageId, categoryId, priority ?? 2),
    onSuccess: (triage) => {
      queryClient.setQueryData(triageKeys.result(triage.message_id), triage);
      void queryClient.invalidateQueries({ queryKey: ["message", "triage", "inbox"] });
    },
  });
}

export interface CorrectTriageInput {
  messageId: string;
  categoryId: string;
  priority?: number | null;
}

function useInvalidateCategories() {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["triage"] }),
      // Hidden or deleted categories change the groups of the inbox.
      queryClient.invalidateQueries({ queryKey: triageKeys.all }),
    ]);
}

export function useCreateCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: (body: CategoryCreate) => unwrap(api.POST("/triage/categories", { body })),
    onSuccess: () => invalidate(),
  });
}

export function useUpdateCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: CategoryUpdate }) =>
      unwrap(
        api.PATCH("/triage/categories/{category_id}", {
          params: { path: { category_id: id } },
          body,
        }),
      ),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.DELETE("/triage/categories/{category_id}", {
          params: { path: { category_id: id } },
        }),
      ),
    onSuccess: () => invalidate(),
  });
}

export function useOrderCategories() {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: (categoryIds: string[]) =>
      unwrap(api.PUT("/triage/categories/order", { body: { category_ids: categoryIds } })),
    // Move at once; the server's answer replaces the guess.
    onMutate: (categoryIds) => {
      const previous = queryClient.getQueryData(categoriesQueryOptions.queryKey);
      if (previous) {
        const byId = new Map(previous.map((category) => [category.id, category]));
        queryClient.setQueryData(
          categoriesQueryOptions.queryKey,
          categoryIds.flatMap((id) => byId.get(id) ?? []),
        );
      }
      return { previous };
    },
    onError: (_error, _ids, context) => {
      if (context?.previous)
        queryClient.setQueryData(categoriesQueryOptions.queryKey, context.previous);
    },
    onSuccess: (categories) => {
      queryClient.setQueryData(categoriesQueryOptions.queryKey, categories);
      void invalidate();
    },
  });
}

export function useCreateOrganizationCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: (body: OrganizationCategoryCreate) =>
      unwrap(api.POST("/triage/organization/categories", { body })),
    onSuccess: () => invalidate(),
  });
}

export function useUpdateOrganizationCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: OrganizationCategoryUpdate }) =>
      unwrap(
        api.PATCH("/triage/organization/categories/{category_id}", {
          params: { path: { category_id: id } },
          body,
        }),
      ),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteOrganizationCategory() {
  const invalidate = useInvalidateCategories();
  return useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.DELETE("/triage/organization/categories/{category_id}", {
          params: { path: { category_id: id } },
        }),
      ),
    onSuccess: () => invalidate(),
  });
}
