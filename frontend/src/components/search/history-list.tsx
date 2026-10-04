import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { History, Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type ConversationSummary,
  conversationsQueryOptions,
  deleteAllConversations,
  deleteConversation,
  searchKeys,
} from "@/api/search";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { formatListDate } from "@/lib/mail-format";
import { cn } from "@/lib/utils";

/** Delete one or all conversations; removes them from the cache right away. */
export function useDeleteConversations(onDeleted?: (id: string | undefined) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string | undefined) =>
      id ? deleteConversation(id) : deleteAllConversations(),
    onSuccess: (_, id) => {
      queryClient.setQueryData<ConversationSummary[]>(searchKeys.conversations, (list) =>
        id ? list?.filter((item) => item.id !== id) : [],
      );
      // The stored conversations themselves (`["rag", "conversations", id]`).
      queryClient.removeQueries({
        predicate: ({ queryKey }) =>
          queryKey.length === 3 &&
          queryKey[0] === "rag" &&
          queryKey[1] === "conversations" &&
          (!id || queryKey[2] === id),
      });
      void queryClient.invalidateQueries({ queryKey: searchKeys.conversations, exact: true });
      onDeleted?.(id);
    },
  });
}

interface HistoryListProps {
  activeId?: string;
  onDeleted?: (id: string | undefined) => void;
  /** Shown when there are no conversations. */
  empty?: ReactNode;
}

/** Earlier questions, newest first; each one and all together can be deleted. */
export function HistoryList({ activeId, onDeleted, empty }: HistoryListProps) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const conversations = useQuery(conversationsQueryOptions);
  const remove = useDeleteConversations(onDeleted);
  const [confirmAll, setConfirmAll] = useState(false);

  if (conversations.isPending) return <ListSkeleton rows={5} />;
  if (conversations.isError)
    return (
      <InlineError
        error={conversations.error}
        onRetry={conversations.refetch}
        retrying={conversations.isFetching}
        className="m-4"
      />
    );
  if (conversations.data.length === 0) {
    if (empty) return empty;
    return (
      <EmptyState
        icon={History}
        title={t("search.history.emptyTitle")}
        description={t("search.history.emptyDescription")}
      />
    );
  }

  return (
    <section aria-labelledby="search-history" className="flex flex-col">
      <div className="flex h-10 items-center gap-2 px-4 md:px-5">
        <h2 id="search-history" className="flex-1 text-xs font-medium text-muted-foreground">
          {t("search.history.title")}
        </h2>
        {confirmAll ? (
          <div className="flex items-center gap-1">
            <span className="text-xs text-muted-foreground">{t("search.history.confirmAll")}</span>
            <Button
              variant="destructive"
              size="xs"
              disabled={remove.isPending}
              onClick={() => remove.mutate(undefined, { onSettled: () => setConfirmAll(false) })}
            >
              {t("search.history.deleteAll")}
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setConfirmAll(false)}>
              {t("search.history.keep")}
            </Button>
          </div>
        ) : (
          <Button
            variant="ghost"
            size="xs"
            className="text-muted-foreground"
            onClick={() => setConfirmAll(true)}
          >
            {t("search.history.deleteAll")}
          </Button>
        )}
      </div>
      <ul aria-labelledby="search-history" className="flex flex-col border-t">
        {conversations.data.map((conversation) => (
          <li
            key={conversation.id}
            className={cn(
              "group flex items-center border-b",
              conversation.id === activeId && "bg-accent",
            )}
          >
            <Link
              to="/search"
              search={{ conversation: conversation.id }}
              aria-current={conversation.id === activeId ? "true" : undefined}
              className="flex min-w-0 flex-1 items-baseline gap-3 py-2.5 pl-4 text-ui outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80 focus-visible:ring-inset md:pl-5"
            >
              <span className="min-w-0 flex-1 truncate">{conversation.title}</span>
              <time
                dateTime={conversation.updated_at}
                className="shrink-0 text-xs text-muted-foreground tabular-nums"
              >
                {formatListDate(conversation.updated_at, i18n.language, timezone)}
              </time>
            </Link>
            <Button
              variant="ghost"
              size="icon-sm"
              className="mx-1 shrink-0 text-muted-foreground"
              disabled={remove.isPending}
              aria-label={t("search.history.delete", { title: conversation.title })}
              title={t("search.history.deleteShort")}
              onClick={() => remove.mutate(conversation.id)}
            >
              <Trash2 />
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
