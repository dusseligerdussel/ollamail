import { useMutation, useQueryClient } from "@tanstack/react-query";

import { setSeen } from "@/api/mail";

import { patchMessage } from "./message-cache";

interface SetSeenInput {
  messageId: string;
  seen: boolean;
}

/**
 * Marks a message read or unread. The list and the thread update at once; the server event
 * (`message.updated`) patches the other tabs and readers of a shared mailbox the same way.
 */
export function useSetSeen() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ messageId, seen }: SetSeenInput) => setSeen(messageId, seen),
    onMutate: ({ messageId, seen }) => patchMessage(queryClient, messageId, { unread: !seen }),
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["message"] });
    },
  });
}
