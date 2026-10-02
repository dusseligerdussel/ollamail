import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";

import { type MessagePage, setSeen, type Thread } from "@/api/mail";

interface SetSeenInput {
  messageId: string;
  seen: boolean;
}

/**
 * Marks a message read or unread. The list and the thread update at once; the server event
 * (`message.updated`) refreshes them afterwards.
 */
export function useSetSeen() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ messageId, seen }: SetSeenInput) => setSeen(messageId, seen),
    onMutate: ({ messageId, seen }) => {
      const unread = !seen;
      queryClient.setQueriesData<InfiniteData<MessagePage>>(
        { queryKey: ["message", "list"] },
        (data) =>
          data && {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.map((item) => (item.id === messageId ? { ...item, unread } : item)),
            })),
          },
      );
      queryClient.setQueriesData<Thread>({ queryKey: ["message", "thread"] }, (thread) =>
        thread?.messages.some((message) => message.id === messageId)
          ? {
              ...thread,
              messages: thread.messages.map((message) =>
                message.id === messageId ? { ...message, unread } : message,
              ),
            }
          : thread,
      );
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["message"] });
    },
  });
}
