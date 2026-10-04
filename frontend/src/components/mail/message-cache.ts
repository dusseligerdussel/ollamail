import type { InfiniteData, QueryClient, QueryKey } from "@tanstack/react-query";

import type { Thread } from "@/api/mail";

/**
 * Lists a message shows up in: the inbox by date and the inbox by category (#21). Both page
 * through `MessageSummary` items.
 */
export const MESSAGE_LIST_KEYS: QueryKey[] = [
  ["message", "list"],
  ["message", "triage", "inbox"],
];

interface ListPage {
  items: { id: string }[];
  total?: number | null;
}

/** Fields of a message that change without reloading it (read state, flag). */
export type MessageStatePatch = Partial<{ unread: boolean; flagged: boolean }>;

/** Updates a message in the loaded lists and threads, without refetching them. */
export function patchMessage(
  queryClient: QueryClient,
  messageId: string,
  patch: MessageStatePatch,
) {
  const update = <T extends { id: string }>(item: T): T =>
    item.id === messageId ? { ...item, ...patch } : item;
  for (const queryKey of MESSAGE_LIST_KEYS) {
    queryClient.setQueriesData<InfiniteData<ListPage>>({ queryKey }, (data) =>
      data?.pages.some((page) => page.items.some((item) => item.id === messageId))
        ? {
            ...data,
            pages: data.pages.map((page) => ({ ...page, items: page.items.map(update) })),
          }
        : data,
    );
  }
  queryClient.setQueriesData<Thread>({ queryKey: ["message", "thread"] }, (thread) =>
    thread?.messages.some((message) => message.id === messageId)
      ? { ...thread, messages: thread.messages.map(update) }
      : thread,
  );
}

/** Removes a message from the loaded lists (archived, trashed or moved). */
export function removeFromLists(queryClient: QueryClient, messageId: string) {
  for (const queryKey of MESSAGE_LIST_KEYS) {
    queryClient.setQueriesData<InfiniteData<ListPage>>({ queryKey }, (data) => {
      if (!data?.pages.some((page) => page.items.some((item) => item.id === messageId))) {
        return data;
      }
      return {
        ...data,
        pages: data.pages.map((page) => ({
          ...page,
          items: page.items.filter((item) => item.id !== messageId),
          // Only the first page carries the total.
          total: typeof page.total === "number" ? Math.max(0, page.total - 1) : page.total,
        })),
      };
    });
  }
}
