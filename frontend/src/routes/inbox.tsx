import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Filter, Inbox, Mail, MailOpen, MailPlus, Settings2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef } from "react";
import { Trans, useTranslation } from "react-i18next";

import {
  foldersQueryOptions,
  type MessageFilters,
  type MessageSummary,
  mailboxesQueryOptions,
  messagesQueryOptions,
  threadQueryOptions,
} from "@/api/mail";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { KeyHint } from "@/components/key-hint";
import { ListSkeleton } from "@/components/list-skeleton";
import { MessageList } from "@/components/mail/message-list";
import { ThreadSkeleton, ThreadView } from "@/components/mail/thread-view";
import { useSetSeen } from "@/components/mail/use-set-seen";
import { PageHeader } from "@/components/page-header";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { SplitView } from "@/components/split-view";
import { TriageControls } from "@/components/triage/triage-controls";
import {
  GROUPED,
  parseCategoryParam,
  TriageViewSelect,
  useTriageInbox,
} from "@/components/triage/triage-inbox";
import { HideTriageLabels } from "@/components/triage/triage-label";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOptGroup,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Toggle } from "@/components/ui/toggle";
import { useListNavigation } from "@/hooks/use-list-navigation";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import type { Command } from "@/lib/commands";

interface InboxSearch {
  mailbox?: string;
  folder?: string;
  unread?: boolean;
  /** Triage view (#21): grouped by category (`all`), one category ID or `none`. */
  category?: string;
  /** The opened message. */
  message?: string;
}

const ID = /^[0-9a-f-]{36}$/i;

function id(value: unknown) {
  return typeof value === "string" && ID.test(value) ? value : undefined;
}

export const Route = createFileRoute("/inbox")({
  validateSearch: (search: Record<string, unknown>): InboxSearch => {
    const mailbox = id(search.mailbox);
    return {
      ...(mailbox && { mailbox }),
      // A folder belongs to one mailbox.
      ...(mailbox && id(search.folder) && { folder: id(search.folder) }),
      ...((search.unread === true || search.unread === "true") && { unread: true }),
      // Categories apply to the inbox folders, not to other folders.
      ...(!search.folder &&
        parseCategoryParam(search.category) && { category: parseCategoryParam(search.category) }),
      ...(id(search.message) && { message: id(search.message) }),
    };
  },
  component: InboxPage,
});

const ACTIVE_PHASES = new Set(["pending", "importing", "syncing"]);

function InboxPage() {
  const { t } = useTranslation();
  const search = Route.useSearch();
  const navigate = useNavigate({ from: "/inbox" });
  const split = useMediaQuery(mediaQueries.split);
  const filters = useMemo<MessageFilters>(
    () => ({ mailbox: search.mailbox, folder: search.folder, unread: search.unread }),
    [search.mailbox, search.folder, search.unread],
  );

  const mailboxes = useQuery(mailboxesQueryOptions);
  const triage = useTriageInbox(search);
  const byDate = useInfiniteQuery({ ...messagesQueryOptions(filters), enabled: !triage.enabled });
  const messages = triage.enabled ? triage.query : byDate;
  const byDateItems = useMemo(
    () => byDate.data?.pages.flatMap((page) => page.items) ?? [],
    [byDate.data],
  );
  const items: MessageSummary[] = triage.enabled ? triage.items : byDateItems;
  const total = triage.enabled ? triage.total : (byDate.data?.pages[0]?.total ?? 0);
  const selectedId = search.message;

  const open = useCallback(
    (messageId: string) =>
      void navigate({ search: (previous) => ({ ...previous, message: messageId }) }),
    [navigate],
  );
  const close = useCallback(
    () => void navigate({ search: ({ message: _, ...rest }) => rest }),
    [navigate],
  );
  const linkSearch = useCallback(
    (message: MessageSummary) => ({ ...search, message: message.id }),
    [search],
  );

  const { activeIndex, setActiveIndex } = useListNavigation({
    count: items.length,
    onOpen: (index) => {
      const message = items[index];
      if (message) open(message.id);
    },
  });
  // Follow the opened message, so `j`/`k` continue from there.
  useEffect(() => {
    if (!selectedId) return;
    const index = items.findIndex((message) => message.id === selectedId);
    if (index >= 0) setActiveIndex(index);
  }, [selectedId, items, setActiveIndex]);

  // Users of a shared mailbox may only read it; read state belongs to the mailbox.
  const canAct = useCallback(
    (mailboxId: string | undefined) =>
      !!mailboxes.data?.find((mailbox) => mailbox.id === mailboxId)?.permissions.includes("act"),
    [mailboxes.data],
  );
  const setSeen = useSetSeen();
  const thread = useQuery({ ...threadQueryOptions(selectedId ?? ""), enabled: !!selectedId });
  const openedMessage = thread.data?.messages.find((message) => message.id === selectedId);
  const listMessage = items.find((message) => message.id === selectedId);
  const openedUnread = listMessage?.unread ?? openedMessage?.unread ?? false;

  // Opening a message marks it read, once per opening (`u` can mark it unread again).
  const markedRead = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!openedMessage || markedRead.current === openedMessage.id) return;
    if (!canAct(openedMessage.mailbox_id)) return;
    markedRead.current = openedMessage.id;
    if (openedMessage.unread) setSeen.mutate({ messageId: openedMessage.id, seen: true });
  }, [openedMessage, setSeen, canAct]);

  const target = selectedId
    ? { id: selectedId, unread: openedUnread, mailbox: thread.data?.mailbox_id }
    : items[activeIndex] && {
        id: items[activeIndex].id,
        unread: items[activeIndex].unread,
        mailbox: items[activeIndex].mailbox_id,
      };
  const toggleTarget = target && canAct(target.mailbox) ? target : undefined;
  const toggleUnread = useCallback(() => {
    if (toggleTarget) setSeen.mutate({ messageId: toggleTarget.id, seen: toggleTarget.unread });
  }, [toggleTarget, setSeen]);

  useShortcut(
    { id: "mail.open", keys: "enter", group: "list", description: t("mail.shortcuts.open") },
    () => {
      const message = items[activeIndex];
      if (message) open(message.id);
    },
  );
  useShortcut(
    {
      id: "mail.close",
      keys: "escape",
      group: "list",
      description: t("mail.shortcuts.close"),
      enabled: !!selectedId,
    },
    close,
  );
  useShortcut(
    {
      id: "mail.toggleUnread",
      keys: "u",
      group: "list",
      description: t("mail.shortcuts.toggleUnread"),
    },
    toggleUnread,
  );

  const setUnreadFilter = useCallback(
    (unread: boolean) =>
      void navigate({
        search: ({ unread: _, message: __, ...rest }) => (unread ? { ...rest, unread } : rest),
      }),
    [navigate],
  );
  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [
      {
        id: "mail.filterUnread",
        label: search.unread ? t("mail.showAll") : t("mail.showUnread"),
        group: "actions",
        icon: Filter,
        run: () => setUnreadFilter(!search.unread),
      },
      {
        id: "mail.manageMailboxes",
        label: t("mailboxes.manage"),
        group: "actions",
        icon: Settings2,
        run: () => void navigate({ to: "/settings/mailboxes" }),
      },
      {
        id: "mail.addMailbox",
        label: t("mailboxes.add"),
        group: "actions",
        icon: MailPlus,
        run: () => void navigate({ to: "/settings/mailboxes/new" }),
      },
    ];
    if (toggleTarget) {
      list.unshift({
        id: "mail.toggleUnread",
        label: toggleTarget.unread ? t("mail.markRead") : t("mail.markUnread"),
        group: "actions",
        icon: toggleTarget.unread ? MailOpen : Mail,
        shortcut: "u",
        run: toggleUnread,
      });
    }
    return list;
  }, [t, search.unread, setUnreadFilter, navigate, toggleTarget, toggleUnread]);
  useCommands(commands);

  const setCategoryView = useCallback(
    (category: string | undefined) =>
      void navigate({
        search: ({ category: _, message: __, ...rest }) =>
          category ? { ...rest, category } : rest,
      }),
    [navigate],
  );
  const setGrouped = useCallback(
    (grouped: boolean) => setCategoryView(grouped ? GROUPED : undefined),
    [setCategoryView],
  );

  const noMailboxes = mailboxes.data?.length === 0;
  const sharedMailbox = mailboxes.data?.find(
    (mailbox) => mailbox.id === search.mailbox && mailbox.is_shared,
  );
  const importing = mailboxes.data?.some((mailbox) => ACTIVE_PHASES.has(mailbox.status.phase));

  let listContent: ReactNode;
  if (noMailboxes) {
    listContent = (
      <EmptyState
        icon={Inbox}
        title={t("mail.noMailboxesTitle")}
        description={t("mail.noMailboxesDescription")}
        action={
          <Button asChild size="sm">
            <Link to="/settings/mailboxes/new">{t("mailboxes.add")}</Link>
          </Button>
        }
      />
    );
  } else if (messages.isPending) {
    listContent = <ListSkeleton rows={12} />;
  } else if (messages.isError) {
    listContent = <InlineError error={messages.error} className="m-4" />;
  } else if (items.length === 0) {
    listContent = (
      <EmptyState
        icon={Inbox}
        title={search.unread ? t("mail.noUnreadTitle") : t("pages.inbox.emptyTitle")}
        description={
          search.unread
            ? t("mail.noUnreadDescription")
            : importing
              ? t("mail.importingDescription")
              : t("mail.emptyDescription")
        }
        action={
          search.unread ? (
            <Button size="sm" variant="outline" onClick={() => setUnreadFilter(false)}>
              {t("mail.showAll")}
            </Button>
          ) : undefined
        }
      />
    );
  } else {
    listContent = (
      <MessageList
        items={items}
        total={total}
        hasNextPage={messages.hasNextPage}
        isFetchingNextPage={messages.isFetchingNextPage}
        fetchNextPage={() => void messages.fetchNextPage()}
        selectedId={selectedId}
        activeIndex={activeIndex}
        linkSearch={linkSearch}
        groupHeader={triage.groupHeader}
      />
    );
  }

  let detail: ReactNode;
  if (!selectedId) {
    detail = (
      <>
        <div aria-hidden="true" className="h-header shrink-0 border-b" />
        <EmptyState
          title={t("pages.inbox.noSelection")}
          description={
            <Trans
              i18nKey="common.navigateHint"
              components={[<KeyHint key="j" keys="j" />, <KeyHint key="k" keys="k" />]}
            />
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
        <InlineError error={thread.error} className="m-4" />
      </>
    );
  } else {
    detail = (
      <ThreadView
        key={selectedId}
        thread={thread.data}
        messageId={selectedId}
        unread={openedUnread}
        onToggleUnread={canAct(thread.data.mailbox_id) ? toggleUnread : undefined}
        onBack={split ? undefined : close}
      />
    );
  }

  return (
    <SplitView
      id="inbox"
      detailOpen={!!selectedId}
      list={
        <>
          <PageHeader
            // A shared mailbox opened from the navigation shows its own name.
            title={sharedMailbox?.display_name ?? t("nav.inbox")}
            meta={total > 0 ? new Intl.NumberFormat().format(total) : undefined}
            actions={
              !noMailboxes && !search.folder ? (
                <TriageViewSelect
                  value={search.category}
                  groups={triage.groups}
                  onChange={setCategoryView}
                />
              ) : undefined
            }
          />
          {!noMailboxes && <FilterBar search={search} onUnreadChange={setUnreadFilter} />}
          <HideTriageLabels hidden={triage.enabled}>{listContent}</HideTriageLabels>
          <TriageControls
            messageId={toggleTarget?.id}
            byCategory={triage.enabled}
            onViewChange={search.folder ? undefined : setGrouped}
          />
        </>
      }
      detail={detail}
    />
  );
}

function FilterBar({
  search,
  onUnreadChange,
}: {
  search: InboxSearch;
  onUnreadChange: (unread: boolean) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate({ from: "/inbox" });
  const mailboxes = useQuery(mailboxesQueryOptions);
  const folders = useQuery({
    ...foldersQueryOptions(search.mailbox ?? ""),
    enabled: !!search.mailbox,
  });
  const synced = folders.data?.filter((folder) => folder.synced && folder.role !== "inbox") ?? [];
  const own = mailboxes.data?.filter((mailbox) => !mailbox.is_shared) ?? [];
  const shared = mailboxes.data?.filter((mailbox) => mailbox.is_shared) ?? [];

  return (
    <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2 md:px-5">
      <NativeSelect
        size="sm"
        className="min-w-0 flex-1 sm:max-w-56"
        aria-label={t("mail.filterMailbox")}
        value={search.mailbox ?? ""}
        onChange={(event) =>
          void navigate({
            search: ({ unread, category }) => ({
              ...(unread && { unread }),
              ...(category && { category }),
              ...(event.target.value && { mailbox: event.target.value }),
            }),
          })
        }
      >
        <NativeSelectOption value="">{t("mail.allMailboxes")}</NativeSelectOption>
        {shared.length === 0 ? (
          own.map((mailbox) => (
            <NativeSelectOption key={mailbox.id} value={mailbox.id}>
              {mailbox.display_name}
            </NativeSelectOption>
          ))
        ) : (
          <>
            {own.length > 0 && (
              <NativeSelectOptGroup label={t("mail.ownMailboxes")}>
                {own.map((mailbox) => (
                  <NativeSelectOption key={mailbox.id} value={mailbox.id}>
                    {mailbox.display_name}
                  </NativeSelectOption>
                ))}
              </NativeSelectOptGroup>
            )}
            <NativeSelectOptGroup label={t("mail.sharedMailboxes")}>
              {shared.map((mailbox) => (
                <NativeSelectOption key={mailbox.id} value={mailbox.id}>
                  {mailbox.display_name}
                </NativeSelectOption>
              ))}
            </NativeSelectOptGroup>
          </>
        )}
      </NativeSelect>
      {search.mailbox && (
        <NativeSelect
          size="sm"
          className="min-w-0 flex-1 sm:max-w-48"
          aria-label={t("mail.filterFolder")}
          value={search.folder ?? ""}
          onChange={(event) =>
            void navigate({
              search: ({ message: _, folder: __, ...rest }) => ({
                ...rest,
                ...(event.target.value && { folder: event.target.value }),
              }),
            })
          }
        >
          <NativeSelectOption value="">{t("mail.inboxFolder")}</NativeSelectOption>
          {synced.map((folder) => (
            <NativeSelectOption key={folder.id} value={folder.id}>
              {folder.name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      )}
      <Toggle
        size="sm"
        variant="outline"
        className="ml-auto shrink-0 text-ui"
        pressed={!!search.unread}
        onPressedChange={onUnreadChange}
      >
        {t("mail.unreadOnly")}
      </Toggle>
    </div>
  );
}
