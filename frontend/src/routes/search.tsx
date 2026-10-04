import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { CircleHelp, Plus, Search, SearchX, Square } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { mailboxesQueryOptions, threadQueryOptions } from "@/api/mail";
import {
  type Conversation,
  conversationQueryOptions,
  type SearchHit,
  searchQueryOptions,
} from "@/api/search";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { KeyHint } from "@/components/key-hint";
import { ListSkeleton } from "@/components/list-skeleton";
import { type MessageFocus, ThreadSkeleton, ThreadView } from "@/components/mail/thread-view";
import { useSetSeen } from "@/components/mail/use-set-seen";
import { PageHeader } from "@/components/page-header";
import { type AnswerTurn, AnswerView, type TurnSource } from "@/components/search/answer-view";
import { HistoryList } from "@/components/search/history-list";
import { HitList } from "@/components/search/hit-list";
import {
  hasFilters,
  type SearchFilterState,
  SearchFilters,
  toFilterParams,
} from "@/components/search/search-filters";
import { type LiveAnswer, useAnswerStream } from "@/components/search/use-answer-stream";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { SplitView } from "@/components/split-view";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useListNavigation } from "@/hooks/use-list-navigation";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import type { Command } from "@/lib/commands";
import { isQuestion, queryTerms } from "@/lib/search-text";
import { cn } from "@/lib/utils";

/**
 * Only IDs live in the URL. The query itself stays in memory: it is mail content and must
 * not end up in access logs or the browser history.
 */
interface SearchParams {
  /** The opened message. */
  message?: string;
  /** The conversation (question and answers) shown. */
  conversation?: string;
}

const ID = /^[0-9a-f-]{36}$/i;

function id(value: unknown) {
  return typeof value === "string" && ID.test(value) ? value : undefined;
}

export const Route = createFileRoute("/search")({
  validateSearch: (search: Record<string, unknown>): SearchParams => ({
    ...(id(search.message) && { message: id(search.message) }),
    ...(id(search.conversation) && { conversation: id(search.conversation) }),
  }),
  component: SearchPage,
});

/** Stored question/answer pairs of a conversation. */
function storedTurns(conversation: Conversation): AnswerTurn[] {
  const turns: AnswerTurn[] = [];
  const { messages } = conversation;
  for (let index = 0; index < messages.length; index += 1) {
    const question = messages[index];
    if (question?.role !== "user") continue;
    const answer = messages[index + 1]?.role === "assistant" ? messages[index + 1] : undefined;
    if (!answer) continue;
    index += 1;
    turns.push({
      key: answer.id,
      question: question.content,
      text: answer.content,
      phase: "done",
      status: answer.status,
      sources: answer.citations,
    });
  }
  return turns;
}

function liveTurn(answer: LiveAnswer): AnswerTurn {
  return {
    key: answer.answerId ?? "live",
    question: answer.question,
    text: answer.text,
    phase: answer.phase,
    status: answer.status,
    sources: answer.sources,
    error: answer.error,
  };
}

interface Submitted {
  query: string;
  /** Asked as a question (answer with sources) rather than searched for hits. */
  question: boolean;
}

function SearchPage() {
  const { t } = useTranslation();
  const params = Route.useSearch();
  const navigate = useNavigate({ from: "/search" });
  const split = useMediaQuery(mediaQueries.split);
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const input = useRef<HTMLInputElement>(null);

  const [draft, setDraft] = useState("");
  const [submitted, setSubmitted] = useState<Submitted | undefined>();
  const [filters, setFilters] = useState<SearchFilterState>({});
  const [focus, setFocus] = useState<MessageFocus | undefined>();
  const filterParams = useMemo(() => toFilterParams(filters), [filters]);
  const stream = useAnswerStream();

  const mailboxes = useQuery(mailboxesQueryOptions);
  const hits = useQuery({
    ...searchQueryOptions(submitted?.query ?? "", filterParams),
    enabled: !!submitted,
  });
  const hitItems = hits.data?.hits ?? [];
  const terms = useMemo(() => queryTerms(submitted?.query ?? ""), [submitted?.query]);
  const conversation = useQuery({
    ...conversationQueryOptions(params.conversation ?? ""),
    // A new conversation is stored with its first answer: load it once that is complete.
    enabled: !!params.conversation && !stream.streaming,
  });

  // `/` opens this page (global shortcut); here it also puts the cursor into the field.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable=true], [role=dialog]")) {
        return;
      }
      event.preventDefault();
      input.current?.focus();
      input.current?.select();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const openMessage = useCallback(
    (nextFocus: MessageFocus) => {
      setFocus(nextFocus);
      void navigate({ search: (previous) => ({ ...previous, message: nextFocus.messageId }) });
    },
    [navigate],
  );
  const openHit = useCallback(
    (hit: SearchHit) =>
      setFocus({
        messageId: hit.message_id,
        passage: hit.excerpt,
        attachmentId: hit.attachment_id,
      }),
    [],
  );
  const openSource = useCallback(
    (source: TurnSource) =>
      openMessage({
        messageId: source.message_id,
        passage: source.snippet,
        attachmentId: source.attachment_id,
      }),
    [openMessage],
  );
  const closeMessage = useCallback(
    () => void navigate({ search: ({ message: _, ...rest }) => rest }),
    [navigate],
  );

  const ask = useCallback(
    (question: string, conversationId: string | undefined) =>
      void stream.ask({
        question,
        conversationId,
        filters: filterParams,
        onConversation: (conversation) =>
          void navigate({ search: (previous) => ({ ...previous, conversation }) }),
      }),
    [stream.ask, filterParams, navigate],
  );

  const submit = useCallback(
    (forceQuestion: boolean) => {
      const query = draft.trim();
      if (!query) return;
      const question = forceQuestion || isQuestion(query);
      setSubmitted({ query, question });
      if (question) {
        // A question while a conversation is shown is a follow-up.
        ask(query, params.conversation);
        setDraft("");
      } else {
        stream.reset();
        void navigate({ search: ({ conversation: _, ...rest }) => rest });
      }
    },
    [draft, ask, params.conversation, stream.reset, navigate],
  );

  // After an error, the same question again (in the same conversation).
  const live = stream.answer;
  const liveKey = live?.phase === "error" ? liveTurn(live).key : undefined;
  const retry = useCallback(() => {
    if (live) ask(live.question, live.conversationId);
  }, [live, ask]);

  const askSubmitted = useCallback(() => {
    if (!submitted) return;
    setSubmitted({ ...submitted, question: true });
    ask(submitted.query, undefined);
  }, [submitted, ask]);

  const newSearch = useCallback(() => {
    stream.reset();
    setSubmitted(undefined);
    setDraft("");
    setFocus(undefined);
    void navigate({ search: {} });
    input.current?.focus();
  }, [stream.reset, navigate]);

  const { activeIndex, setActiveIndex } = useListNavigation({
    count: hitItems.length,
    onOpen: (index) => {
      const hit = hitItems[index];
      if (hit)
        openMessage({
          messageId: hit.message_id,
          passage: hit.excerpt,
          attachmentId: hit.attachment_id,
        });
    },
  });
  useEffect(() => {
    if (!params.message) return;
    const index = hitItems.findIndex((hit) => hit.message_id === params.message);
    if (index >= 0) setActiveIndex(index);
  }, [params.message, hitItems, setActiveIndex]);

  // Only after `j`/`k` chose a hit: otherwise Enter belongs to the focused control.
  useShortcut(
    {
      id: "search.openHit",
      keys: "enter",
      group: "list",
      description: t("search.shortcuts.open"),
      enabled: activeIndex >= 0,
    },
    () => {
      const hit = hitItems[activeIndex];
      if (hit)
        openMessage({
          messageId: hit.message_id,
          passage: hit.excerpt,
          attachmentId: hit.attachment_id,
        });
    },
  );
  useShortcut(
    {
      id: "search.cancel",
      keys: "escape",
      group: "list",
      description: t("search.answer.cancel"),
      allowInInput: true,
      enabled: stream.streaming,
    },
    stream.cancel,
  );
  useShortcut(
    {
      id: "search.close",
      keys: "escape",
      group: "list",
      description: t("mail.shortcuts.close"),
      enabled: !!params.message && !stream.streaming,
    },
    closeMessage,
  );

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [
      {
        id: "search.new",
        label: t("search.new"),
        group: "actions",
        icon: Plus,
        run: newSearch,
      },
    ];
    if (stream.streaming) {
      list.unshift({
        id: "search.cancel",
        label: t("search.answer.cancel"),
        group: "actions",
        icon: Square,
        shortcut: "escape",
        run: stream.cancel,
      });
    }
    if (submitted && !submitted.question) {
      list.push({
        id: "search.askSubmitted",
        label: t("search.askInstead"),
        group: "actions",
        icon: CircleHelp,
        run: askSubmitted,
      });
    }
    return list;
  }, [t, newSearch, stream.streaming, stream.cancel, submitted, askSubmitted]);
  useCommands(commands);

  // Mark opened messages read, like the inbox does.
  const setSeen = useSetSeen();
  const thread = useQuery({
    ...threadQueryOptions(params.message ?? ""),
    enabled: !!params.message,
  });
  const openedMessage = thread.data?.messages.find((message) => message.id === params.message);
  const markedRead = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!openedMessage || markedRead.current === openedMessage.id) return;
    markedRead.current = openedMessage.id;
    if (openedMessage.unread) setSeen.mutate({ messageId: openedMessage.id, seen: true });
  }, [openedMessage, setSeen]);
  const toggleUnread = useCallback(() => {
    if (openedMessage) setSeen.mutate({ messageId: openedMessage.id, seen: openedMessage.unread });
  }, [openedMessage, setSeen]);

  // Answers: the stored conversation plus the answer being streamed (until it is stored).
  const turns = useMemo(() => {
    const stored = conversation.data && params.conversation ? storedTurns(conversation.data) : [];
    const live = stream.answer;
    if (
      live &&
      (!live.conversationId || live.conversationId === params.conversation) &&
      !stored.some((turn) => turn.key === live.answerId)
    ) {
      stored.push(liveTurn(live));
    }
    return stored;
  }, [conversation.data, params.conversation, stream.answer]);

  const noMailboxes = mailboxes.data?.length === 0;
  // Below an answer, hits are extra context: left out while loading or when there are none.
  const showHits =
    !!submitted &&
    (!submitted.question || (turns.length > 0 && (hits.isError || hitItems.length > 0)));

  // Side by side, the answer gets the wide detail pane while no mail is open there.
  const answersInDetail = split && !params.message;
  let answers: ReactNode = null;
  if (params.conversation && conversation.isPending && turns.length === 0) {
    answers = <ListSkeleton rows={3} />;
  } else if (params.conversation && conversation.isError && turns.length === 0) {
    answers = (
      <InlineError
        error={conversation.error}
        onRetry={conversation.refetch}
        retrying={conversation.isFetching}
        className="m-4"
      />
    );
  } else if (turns.length > 0) {
    answers = (
      <section
        aria-label={t("search.answer.title")}
        className={cn("flex flex-col divide-y", !answersInDetail && "border-b")}
      >
        {turns.map((turn) => (
          <AnswerView
            key={turn.key}
            turn={turn}
            onOpenSource={openSource}
            onCancel={stream.cancel}
            onRetry={turn.key === liveKey ? retry : undefined}
            headingLevel={answersInDetail ? 3 : 2}
          />
        ))}
      </section>
    );
  }
  const listAnswers = answersInDetail ? null : answers;

  let hitContent: ReactNode = null;
  if (showHits) {
    if (hits.isPending) {
      hitContent = <ListSkeleton rows={6} />;
    } else if (hits.isError) {
      hitContent = (
        <InlineError
          error={hits.error}
          onRetry={hits.refetch}
          retrying={hits.isFetching}
          className="m-4"
        />
      );
    } else if (hitItems.length === 0) {
      hitContent = (
        <EmptyState
          icon={SearchX}
          title={t("search.noHitsTitle")}
          description={t("search.noHitsDescription")}
          action={
            hasFilters(filters) ? (
              <Button size="sm" variant="outline" onClick={() => setFilters({})}>
                {t("search.filters.reset")}
              </Button>
            ) : undefined
          }
          className="h-auto"
        />
      );
    } else {
      hitContent = (
        <HitList
          hits={hitItems}
          terms={terms}
          selectedId={params.message}
          activeIndex={activeIndex}
          linkSearch={(hit) => ({ ...params, message: hit.message_id })}
          onOpen={openHit}
        />
      );
    }
  }

  let content: ReactNode;
  if (noMailboxes) {
    content = (
      <EmptyState
        icon={Search}
        title={t("search.noMailboxesTitle")}
        description={t("search.noMailboxesDescription")}
        action={
          <Button asChild size="sm">
            <Link to="/settings/mailboxes/new">{t("mailboxes.add")}</Link>
          </Button>
        }
      />
    );
  } else if (!listAnswers && !showHits) {
    content = (
      <HistoryList
        activeId={params.conversation}
        empty={
          <EmptyState
            icon={Search}
            title={t("search.idleTitle")}
            description={t("search.idleDescription")}
          />
        }
      />
    );
  } else {
    content = (
      <>
        {listAnswers}
        {showHits && (
          <section aria-labelledby="search-hits" className="flex flex-col">
            <div className="flex h-10 shrink-0 items-center gap-2 px-4 md:px-5">
              <h2 id="search-hits" className="text-xs font-medium text-muted-foreground">
                {t("search.hits")}
                {hits.data && hitItems.length > 0 && (
                  <span className="ml-1.5 tabular-nums">{hitItems.length}</span>
                )}
              </h2>
              {submitted && !submitted.question && (
                <Button
                  variant="ghost"
                  size="xs"
                  className="ml-auto text-muted-foreground"
                  onClick={askSubmitted}
                >
                  <CircleHelp aria-hidden />
                  {t("search.askInstead")}
                </Button>
              )}
            </div>
            <div className="border-t">{hitContent}</div>
          </section>
        )}
      </>
    );
  }

  const active = !!submitted || !!params.conversation || !!stream.answer;
  const list = (
    <>
      <PageHeader
        title={t("nav.search")}
        actions={
          active ? (
            <Button variant="ghost" size="sm" onClick={newSearch}>
              <Plus aria-hidden />
              {t("search.new")}
            </Button>
          ) : undefined
        }
      />
      <search className="shrink-0 border-b" onFocus={() => setActiveIndex(-1)}>
        <form
          className="flex flex-col gap-2 px-4 py-3 md:px-5"
          onSubmit={(event) => {
            event.preventDefault();
            submit(false);
          }}
        >
          <div className="relative">
            <Search
              aria-hidden
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
            />
            <Input
              ref={input}
              type="search"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  submit(true);
                }
              }}
              // Only where a keyboard is likely: on phones it would open the keyboard at once.
              autoFocus={hasKeyboard && !params.message}
              maxLength={2000}
              enterKeyHint="search"
              aria-label={t("search.inputLabel")}
              placeholder={
                params.conversation ? t("search.followUpPlaceholder") : t("search.placeholder")
              }
              className="h-9 pl-8"
            />
          </div>
          <SearchFilters filters={filters} onChange={setFilters} />
          {hasKeyboard && (
            <p className="text-xs text-muted-foreground">
              <Trans
                i18nKey="search.keyHint"
                components={[
                  <KeyHint key="enter" keys="enter" />,
                  <KeyHint key="ask" keys="mod+enter" />,
                ]}
              />
            </p>
          )}
        </form>
      </search>
      <div className="min-h-0 flex-1 overflow-y-auto">{content}</div>
    </>
  );

  let detail: ReactNode;
  if (answersInDetail && answers) {
    detail = (
      <>
        <PageHeader title={t("search.answer.title")} headingLevel={2} />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="max-w-3xl">{answers}</div>
        </div>
      </>
    );
  } else if (!params.message) {
    detail = (
      <>
        <div aria-hidden="true" className="h-header shrink-0 border-b" />
        <EmptyState
          title={t("pages.inbox.noSelection")}
          description={
            hasKeyboard ? (
              <Trans
                i18nKey="search.detailHint"
                components={[<KeyHint key="j" keys="j" />, <KeyHint key="k" keys="k" />]}
              />
            ) : (
              t("search.detailHintTouch")
            )
          }
        />
      </>
    );
  } else if (thread.isPending) {
    detail = <ThreadSkeleton />;
  } else if (thread.isError) {
    detail = (
      <>
        <div aria-hidden="true" className="h-header shrink-0 border-b" />
        <InlineError
          error={thread.error}
          onRetry={thread.refetch}
          retrying={thread.isFetching}
          className="m-4"
        />
      </>
    );
  } else {
    detail = (
      <ThreadView
        key={`${params.message}:${focus?.passage ?? ""}:${focus?.attachmentId ?? ""}`}
        thread={thread.data}
        messageId={params.message}
        unread={openedMessage?.unread ?? false}
        onToggleUnread={toggleUnread}
        onBack={split ? undefined : closeMessage}
        focus={focus?.messageId === params.message ? focus : undefined}
      />
    );
  }

  return <SplitView id="search" detailOpen={!!params.message} list={list} detail={detail} />;
}
