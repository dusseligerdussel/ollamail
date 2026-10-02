import { Link } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Paperclip } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";

import type { MessageSummary } from "@/api/mail";
import { TriageLabelSlot } from "@/components/mail/slots";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import { addressName, formatListDate } from "@/lib/mail-format";
import { cn } from "@/lib/utils";

// Row heights in px: `h-row` (one line) on wide screens, two lines on phones.
const ROW_HEIGHT = 36;
const ROW_HEIGHT_TWO_LINES = 60;
const GROUP_HEADER_HEIGHT = 32;
// Rows rendered outside the viewport; loading the next page starts this close to the end.
const OVERSCAN = 12;

interface MessageListProps {
  items: MessageSummary[];
  /** All matching messages; rows beyond `items` show placeholders until they are loaded. */
  total: number;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  selectedId?: string;
  /** Keyboard position (`j`/`k`), -1 for none. */
  activeIndex: number;
  /** Search params of a row link (keeps the filters, opens the message). */
  linkSearch: (message: MessageSummary) => Record<string, unknown>;
  /**
   * Optional group headings (e.g. by triage category): returns a heading if a new group starts
   * at `message`, which follows `previous` in the list.
   */
  groupHeader?: (message: MessageSummary, previous: MessageSummary | undefined) => ReactNode;
}

type Row =
  | { type: "message"; index: number }
  | { type: "header"; key: string; content: ReactNode }
  | { type: "placeholder"; index: number };

/** Virtualised inbox list: one row per message, smooth with tens of thousands of rows. */
export function MessageList({
  items,
  total,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  selectedId,
  activeIndex,
  linkSearch,
  groupHeader,
}: MessageListProps) {
  const { t } = useTranslation();
  const oneLine = useMediaQuery(mediaQueries.sidebar);
  const scrollRef = useRef<HTMLDivElement>(null);
  const count = hasNextPage ? Math.max(total, items.length + 1) : items.length;
  // Messages, interleaved with group headings if any; `rowOf` maps a message to its row.
  const { rows, rowOf } = useMemo(() => {
    const rows: Row[] = [];
    const rowOf: number[] = [];
    items.forEach((message, index) => {
      const header = groupHeader?.(message, items[index - 1]);
      if (header) rows.push({ type: "header", key: `header-${message.id}`, content: header });
      rowOf.push(rows.length);
      rows.push({ type: "message", index });
    });
    for (let index = items.length; index < count; index++)
      rows.push({ type: "placeholder", index });
    return { rows, rowOf };
  }, [items, count, groupHeader]);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) =>
      rows[index]?.type === "header"
        ? GROUP_HEADER_HEIGHT
        : oneLine
          ? ROW_HEIGHT
          : ROW_HEIGHT_TWO_LINES,
    overscan: OVERSCAN,
    // Used until the scroll container is measured.
    initialRect: { width: 800, height: 720 },
    getItemKey: (index) => {
      const row = rows[index];
      if (row?.type === "header") return row.key;
      if (row?.type === "message") return items[row.index]?.id ?? index;
      return `placeholder-${row?.index ?? index}`;
    },
  });

  // Row height depends on the layout; re-measure when it changes.
  const measure = virtualizer.measure;
  useEffect(() => {
    if (oneLine !== undefined) measure();
  }, [oneLine, measure]);

  const virtualItems = virtualizer.getVirtualItems();
  const lastIndex = virtualItems.at(-1)?.index ?? 0;
  const loadedRows = items.length > 0 ? (rowOf[items.length - 1] ?? 0) + 1 : 0;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && lastIndex >= loadedRows - OVERSCAN) {
      fetchNextPage();
    }
  }, [hasNextPage, isFetchingNextPage, lastIndex, loadedRows, fetchNextPage]);

  const activeRow = activeIndex >= 0 ? (rowOf[activeIndex] ?? activeIndex) : -1;
  useEffect(() => {
    if (activeRow >= 0) virtualizer.scrollToIndex(activeRow, { align: "auto" });
  }, [activeRow, virtualizer]);

  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="message-list">
      <ul
        aria-label={t("mail.listLabel")}
        className="relative w-full"
        style={{ height: virtualizer.getTotalSize() }}
      >
        {virtualItems.map((virtualRow) => {
          const row = rows[virtualRow.index];
          const style = { height: virtualRow.size, transform: `translateY(${virtualRow.start}px)` };
          if (row?.type === "header") {
            return (
              <li key={virtualRow.key} className="absolute top-0 left-0 w-full" style={style}>
                {row.content}
              </li>
            );
          }
          const index = row?.index ?? virtualRow.index;
          const message = row?.type === "message" ? items[index] : undefined;
          return (
            <li
              key={virtualRow.key}
              aria-setsize={count}
              aria-posinset={index + 1}
              className="absolute top-0 left-0 w-full"
              style={style}
            >
              {message ? (
                <MessageRow
                  message={message}
                  oneLine={oneLine}
                  selected={message.id === selectedId}
                  active={index === activeIndex}
                  search={linkSearch(message)}
                />
              ) : (
                <PlaceholderRow />
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function MessageRow({
  message,
  oneLine,
  selected,
  active,
  search,
}: {
  message: MessageSummary;
  oneLine: boolean;
  selected: boolean;
  active: boolean;
  search: Record<string, unknown>;
}) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const sender = addressName(message.sender) || t("mail.unknownSender");
  const subject = message.subject || t("mail.noSubject");
  const date = formatListDate(message.date, i18n.language, timezone);

  return (
    <Link
      to="/inbox"
      search={search}
      aria-current={selected ? "true" : undefined}
      data-active={active || undefined}
      data-unread={message.unread || undefined}
      className={cn(
        "group flex h-full border-b border-border/60 px-4 text-ui outline-none",
        "hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset",
        "data-active:shadow-[inset_2px_0_0_var(--ring)]",
        selected && "bg-accent hover:bg-accent",
        oneLine ? "items-center gap-3" : "flex-col justify-center gap-0.5",
      )}
    >
      {oneLine ? (
        <>
          <UnreadDot unread={message.unread} />
          <span className={cn("w-40 shrink-0 truncate", message.unread && "font-semibold")}>
            {sender}
          </span>
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <TriageLabelSlot messageId={message.id} compact />
            <span className="min-w-0 truncate">
              <span className={cn(message.unread && "font-semibold")}>{subject}</span>
              {message.snippet && (
                <span className="text-muted-foreground"> – {message.snippet}</span>
              )}
            </span>
          </span>
          {message.has_attachments && (
            <Paperclip
              aria-label={t("mail.hasAttachments")}
              className="size-3.5 shrink-0 text-muted-foreground"
            />
          )}
          <span className="w-14 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
            {date}
          </span>
        </>
      ) : (
        <>
          <span className="flex items-center gap-2">
            <UnreadDot unread={message.unread} />
            <span className={cn("min-w-0 flex-1 truncate", message.unread && "font-semibold")}>
              {sender}
            </span>
            {message.has_attachments && (
              <Paperclip
                aria-label={t("mail.hasAttachments")}
                className="size-3.5 shrink-0 text-muted-foreground"
              />
            )}
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{date}</span>
          </span>
          <span className="flex min-w-0 items-center gap-2 pl-4">
            <TriageLabelSlot messageId={message.id} compact />
            <span className="min-w-0 truncate">
              <span className={cn(message.unread && "font-medium")}>{subject}</span>
              {message.snippet && (
                <span className="text-muted-foreground"> – {message.snippet}</span>
              )}
            </span>
          </span>
        </>
      )}
    </Link>
  );
}

function UnreadDot({ unread }: { unread: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="flex w-2 shrink-0 justify-center">
      {unread && (
        <span role="img" aria-label={t("mail.unread")} className="size-2 rounded-full bg-brand" />
      )}
    </span>
  );
}

function PlaceholderRow() {
  return (
    <div
      aria-hidden="true"
      className="flex h-full items-center gap-4 border-b border-border/60 px-4"
    >
      <Skeleton className="h-3 w-32 shrink-0" />
      <Skeleton className="h-3 flex-1" />
      <Skeleton className="h-3 w-10 shrink-0" />
    </div>
  );
}
