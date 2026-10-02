import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Eye, ImageOff, Mail, MailOpen, Paperclip } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type Attachment,
  attachmentUrl,
  bodyWithImagesQueryOptions,
  type MessageDetail,
  type Thread,
} from "@/api/mail";
import { InlineError } from "@/components/inline-error";
import { MailBodyFrame } from "@/components/mail/mail-body-frame";
import { MessageTasksSlot, ReplySlot, TriageLabelSlot } from "@/components/mail/slots";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import { findPassage } from "@/lib/find-passage";
import {
  addressFull,
  addressName,
  formatDateTime,
  formatListDate,
  formatSize,
} from "@/lib/mail-format";
import { cn } from "@/lib/utils";

/** Where to open a message: a passage of its text or one of its attachments (search). */
export interface MessageFocus {
  messageId: string;
  passage?: string;
  attachmentId?: string | null;
}

interface ThreadViewProps {
  thread: Thread;
  /** The message opened from the list; its read state is toggled in the header. */
  messageId: string;
  unread: boolean;
  /** Missing for read-only (shared) mailboxes: read state cannot be changed there. */
  onToggleUnread?: () => void;
  /** Back to the list (stacked mobile layout only). */
  onBack?: () => void;
  /** Mark and scroll to this place of the opened message. */
  focus?: MessageFocus;
}

export function ThreadView({
  thread,
  messageId,
  unread,
  onToggleUnread,
  onBack,
  focus,
}: ThreadViewProps) {
  const { t } = useTranslation();
  const last = thread.messages.at(-1)?.id;
  // The opened and the newest message start expanded, older ones show a one-line summary.
  const [expanded, setExpanded] = useState(() => new Set([messageId, last]));

  return (
    <>
      <PageHeader
        title={thread.subject || t("mail.noSubject")}
        // Next to the list (no back button) the list pane has the page's `h1`.
        headingLevel={onBack ? 1 : 2}
        documentTitle={false}
        leading={
          onBack && (
            <Button variant="ghost" size="icon-sm" onClick={onBack} aria-label={t("mail.back")}>
              <ArrowLeft />
            </Button>
          )
        }
        actions={
          onToggleUnread ? (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onToggleUnread}
              aria-label={unread ? t("mail.markRead") : t("mail.markUnread")}
              title={unread ? t("mail.markRead") : t("mail.markUnread")}
            >
              {unread ? <MailOpen /> : <Mail />}
            </Button>
          ) : (
            <span
              className="flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground"
              title={t("mail.readOnlyHint")}
            >
              <Eye aria-hidden className="size-3.5" />
              <span className="max-md:sr-only">{t("mail.readOnly")}</span>
            </span>
          )
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-4 md:px-6">
          <TriageLabelSlot messageId={messageId} />
          {thread.messages.map((message) =>
            expanded.has(message.id) ? (
              <ThreadMessage
                key={message.id}
                message={message}
                focus={focus?.messageId === message.id ? focus : undefined}
              />
            ) : (
              <CollapsedMessage
                key={message.id}
                message={message}
                onExpand={() => setExpanded((ids) => new Set(ids).add(message.id))}
              />
            ),
          )}
          <ReplySlot messageId={messageId} mailboxId={thread.mailbox_id} />
          <MessageTasksSlot messageId={messageId} />
        </div>
      </div>
    </>
  );
}

function CollapsedMessage({ message, onExpand }: { message: MessageDetail; onExpand: () => void }) {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  return (
    <button
      type="button"
      onClick={onExpand}
      className="flex w-full items-center gap-3 rounded-lg border px-4 py-2.5 text-left text-ui outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
    >
      <span className={cn("shrink-0 truncate", message.unread && "font-semibold")}>
        {addressName(message.sender)}
      </span>
      <span className="min-w-0 flex-1 truncate text-muted-foreground">{message.snippet}</span>
      <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
        {formatListDate(message.date, i18n.language, timezone)}
      </span>
    </button>
  );
}

function ThreadMessage({ message, focus }: { message: MessageDetail; focus?: MessageFocus }) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const recipients = [...message.to, ...message.cc];
  const files = message.attachments.filter((attachment) => !attachment.is_inline);

  return (
    <article
      aria-label={t("mail.messageFrom", { sender: addressName(message.sender) })}
      className="rounded-lg border"
    >
      <header className="flex flex-col gap-0.5 px-4 pt-3 pb-2 sm:flex-row sm:items-start sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="truncate text-ui">
            <span className="font-medium">{addressName(message.sender)}</span>
            {message.sender?.name && (
              <span className="text-muted-foreground"> &lt;{message.sender.address}&gt;</span>
            )}
          </div>
          {recipients.length > 0 && (
            <div className="truncate text-xs text-muted-foreground">
              {t("mail.to")} {recipients.map(addressFull).join(", ")}
            </div>
          )}
        </div>
        <time
          dateTime={message.date}
          className="shrink-0 text-xs text-muted-foreground tabular-nums"
        >
          {formatDateTime(message.date, i18n.language, timezone)}
        </time>
      </header>
      <div className="px-4 pb-4">
        <MessageContent message={message} passage={focus?.passage} />
        {files.length > 0 && (
          <AttachmentList
            messageId={message.id}
            attachments={files}
            focusId={focus?.attachmentId ?? undefined}
          />
        )}
      </div>
    </article>
  );
}

/** Plain-text body; `passage` is marked and scrolled into view. */
function PlainText({ text, passage }: { text: string; passage?: string }) {
  const mark = useRef<HTMLElement>(null);
  const found = useMemo(() => (passage ? findPassage(text, passage) : undefined), [text, passage]);
  useEffect(() => {
    if (found) mark.current?.scrollIntoView?.({ block: "center" });
  }, [found]);
  if (!found) return text;
  return (
    <>
      {text.slice(0, found[0])}
      <mark ref={mark} className="rounded-sm bg-brand/15 text-foreground">
        {text.slice(found[0], found[1])}
      </mark>
      {text.slice(found[1])}
    </>
  );
}

function MessageContent({ message, passage }: { message: MessageDetail; passage?: string }) {
  const { t } = useTranslation();
  const [loadImages, setLoadImages] = useState(false);
  const withImages = useQuery({
    ...bodyWithImagesQueryOptions(message.id),
    enabled: loadImages,
  });

  if (message.body.html === null) {
    return (
      <div className="text-sm leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
        {message.text ? (
          <PlainText text={message.text} passage={passage} />
        ) : (
          <span className="text-muted-foreground">{t("mail.emptyBody")}</span>
        )}
      </div>
    );
  }

  const blocked = message.body.blocked_images;
  const html = withImages.data?.html ?? message.body.html;
  return (
    <div className="flex flex-col gap-2">
      {blocked > 0 && !withImages.data && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-muted px-3 py-2 text-ui">
          <ImageOff aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 text-muted-foreground">
            {t("mail.imagesBlocked", { count: blocked })}
          </span>
          <Button
            variant="outline"
            size="xs"
            disabled={withImages.isFetching}
            onClick={() => setLoadImages(true)}
          >
            {t("mail.loadImages")}
          </Button>
        </div>
      )}
      {withImages.isError && <InlineError error={withImages.error} />}
      <MailBodyFrame
        html={html}
        externalImages={Boolean(withImages.data)}
        title={message.subject || t("mail.noSubject")}
        passage={passage}
      />
    </div>
  );
}

function AttachmentList({
  messageId,
  attachments,
  focusId,
}: {
  messageId: string;
  attachments: Attachment[];
  /** The attachment a search hit or a cited source came from. */
  focusId?: string;
}) {
  const { t, i18n } = useTranslation();
  const focused = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    if (focusId) focused.current?.scrollIntoView?.({ block: "center" });
  }, [focusId]);
  return (
    <section aria-label={t("mail.attachments")} className="mt-3">
      <ul className="flex flex-wrap gap-2">
        {attachments.map((attachment) => (
          <li key={attachment.id}>
            <a
              ref={attachment.id === focusId ? focused : undefined}
              href={attachmentUrl(messageId, attachment.id)}
              download={attachment.filename ?? true}
              aria-describedby={attachment.id === focusId ? `found-${attachment.id}` : undefined}
              className={cn(
                "flex max-w-64 items-center gap-2 rounded-md border px-2.5 py-1.5 text-ui outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50",
                attachment.id === focusId && "border-brand/50 bg-brand/10",
              )}
            >
              <Paperclip aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">
                {attachment.filename || t("mail.unnamedAttachment")}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatSize(attachment.size, i18n.language)}
              </span>
              {attachment.id === focusId && (
                <span id={`found-${attachment.id}`} className="sr-only">
                  {t("mail.foundInAttachment")}
                </span>
              )}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Placeholder while the thread loads. */
export function ThreadSkeleton() {
  const { t } = useTranslation();
  return (
    <div role="status" aria-busy="true" className="flex flex-1 flex-col">
      <span className="sr-only">{t("common.loading")}</span>
      <div className="flex h-header shrink-0 items-center border-b px-4 md:px-5">
        <Skeleton className="h-4 w-64" />
      </div>
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-4 md:px-6">
        <div className="rounded-lg border p-4">
          <Skeleton className="mb-2 h-3 w-40" />
          <Skeleton className="mb-6 h-3 w-56" />
          <Skeleton className="mb-2 h-3 w-full" />
          <Skeleton className="mb-2 h-3 w-5/6" />
          <Skeleton className="h-3 w-2/3" />
        </div>
      </div>
    </div>
  );
}
