import {
  type InfiniteData,
  type QueryClient,
  type QueryKey,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { describeApiError } from "@/api/errors";
import {
  type MessageAction,
  type MessagePage,
  problemErrorCode,
  runMessageAction,
  setFlagged,
  type Thread,
} from "@/api/mail";

/** Lists a moved message disappears from: the inbox and the triage view (#21). */
const LIST_KEYS: QueryKey[] = [
  ["message", "list"],
  ["message", "triage", "inbox"],
];

interface ListPage {
  items: { id: string }[];
  total?: number | null;
}

type Snapshot = [QueryKey, unknown][];

function snapshot(queryClient: QueryClient): Snapshot {
  return LIST_KEYS.flatMap((queryKey) => queryClient.getQueriesData({ queryKey }));
}

function restore(queryClient: QueryClient, saved: Snapshot | undefined) {
  for (const [queryKey, data] of saved ?? []) queryClient.setQueryData(queryKey, data);
}

function removeFromLists(queryClient: QueryClient, messageId: string) {
  for (const queryKey of LIST_KEYS) {
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

/** Error codes of `POST /messages/{id}/actions` with their own text. */
const ACTION_ERRORS = new Set([
  "no_archive_folder",
  "no_trash_folder",
  "message_not_found",
  "read_only",
  "unknown_folder",
]);

export interface MoveInput {
  messageId: string;
  action: MessageAction;
  /** Target of `move`. */
  folderId?: string;
  /** Undo of an earlier action: no toast offering to undo again. */
  undo?: boolean;
}

/**
 * Archive, trash and move (#148): the message leaves the lists at once; if the server refuses,
 * it comes back and a toast says why. The success toast offers to undo the action.
 */
export function useMessageActions() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const move = useMutation({
    mutationFn: ({ messageId, action, folderId }: MoveInput) =>
      runMessageAction(messageId, action, folderId),
    meta: { errorToast: false },
    onMutate: async ({ messageId, undo }) => {
      await Promise.all(LIST_KEYS.map((queryKey) => queryClient.cancelQueries({ queryKey })));
      const saved = snapshot(queryClient);
      if (!undo) removeFromLists(queryClient, messageId);
      return saved;
    },
    onError: (error, _input, saved) => {
      restore(queryClient, saved);
      const code = problemErrorCode(error);
      if (code && ACTION_ERRORS.has(code)) toast.error(t(`mail.actions.errors.${code}` as never));
      else toast.error(describeApiError(error, t).title);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["message"] });
    },
  });

  const flag = useMutation({
    mutationFn: ({ messageId, flagged }: { messageId: string; flagged: boolean }) =>
      setFlagged(messageId, flagged),
    onMutate: ({ messageId, flagged }) => {
      const update = <T extends { id: string; flagged: boolean }>(item: T) =>
        item.id === messageId ? { ...item, flagged } : item;
      queryClient.setQueriesData<InfiniteData<MessagePage>>(
        { queryKey: ["message", "list"] },
        (data) =>
          data && {
            ...data,
            pages: data.pages.map((page) => ({ ...page, items: page.items.map(update) })),
          },
      );
      queryClient.setQueriesData<Thread>({ queryKey: ["message", "thread"] }, (thread) =>
        thread?.messages.some((message) => message.id === messageId)
          ? { ...thread, messages: thread.messages.map(update) }
          : thread,
      );
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["message"] });
    },
  });

  const { mutate: moveMutate } = move;
  const { mutate: flagMutate } = flag;
  return useMemo(() => {
    const run = (input: MoveInput) =>
      moveMutate(input, {
        onSuccess: (result) => {
          if (input.undo) return;
          const undoFolder = result.undo_folder_id;
          toast(t(`mail.actions.done.${input.action}`), {
            id: `message-action-${input.messageId}`,
            action: undoFolder
              ? {
                  label: t("mail.actions.undo"),
                  onClick: () =>
                    moveMutate({
                      messageId: input.messageId,
                      action: "move",
                      folderId: undoFolder,
                      undo: true,
                    }),
                }
              : undefined,
          });
        },
      });
    return {
      archive: (messageId: string) => run({ messageId, action: "archive" }),
      trash: (messageId: string) => run({ messageId, action: "trash" }),
      moveTo: (messageId: string, folderId: string) => run({ messageId, action: "move", folderId }),
      setFlagged: (messageId: string, flagged: boolean) => flagMutate({ messageId, flagged }),
    };
  }, [moveMutate, flagMutate, t]);
}
