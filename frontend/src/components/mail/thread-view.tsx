import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ImageOff, Mail, MailOpen, Paperclip } from "lucide-react";
import { useState } from "react";
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
import { MessageTasksSlot, TriageLabelSlot } from "@/components/mail/slots";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import {
  addressFull,
  addressName,
  formatDateTime,
  formatListDate,
  formatSize,
} from "@/lib/mail-format";
import { cn } from "@/lib/utils";

interface ThreadViewProps {
  thread: Thread;
  /** The message opened from the list; its read state is toggled in the header. */
  messageId: string;
  unread: boolean;
  /** Missing for read-only (shared) mailboxes: read state cannot be changed there. */
  onToggleUnread?: () => void;
  /** Back to the list (stacked mobile layout only). */
  onBack?: () => void;
}

export function ThreadView({ thread, messageId, unread, onToggleUnread, onBack }: ThreadViewProps) {
  const { t } = useTranslation();
  const last = thread.messages.at(-1)?.id;
  // The opened and the newest message start expanded, older ones show a one-line summary.
  const [expanded, setExpanded] = useState(() => new Set([messageId, last]));

  return (
    <>
      <PageHeader
        title={thread.subject || t("mail.noSubject")}
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
            <span className="text-xs text-muted-foreground" title={t("mail.readOnlyHint")}>
              {t("mail.readOnly")}
            </span>
          )
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-4 md:px-6">
          <TriageLabelSlot messageId={messageId} />
          {thread.messages.map((message) =>
            expanded.has(message.id) ? (
              <ThreadMessage key={message.id} message={message} />
            ) : (
              <CollapsedMessage
                key={message.id}
                message={message}
                onExpand={() => setExpanded((ids) => new Set(ids).add(message.id))}
              />
            ),
          )}
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

function ThreadMessage({ message }: { message: MessageDetail }) {
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
        <MessageContent message={message} />
        {files.length > 0 && <AttachmentList messageId={message.id} attachments={files} />}
      </div>
    </article>
  );
}

function MessageContent({ message }: { message: MessageDetail }) {
  const { t } = useTranslation();
  const [loadImages, setLoadImages] = useState(false);
  const withImages = useQuery({
    ...bodyWithImagesQueryOptions(message.id),
    enabled: loadImages,
  });

  if (message.body.html === null) {
    return (
      <div className="text-sm leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
        {message.text || <span className="text-muted-foreground">{t("mail.emptyBody")}</span>}
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
      />
    </div>
  );
}

function AttachmentList({
  messageId,
  attachments,
}: {
  messageId: string;
  attachments: Attachment[];
}) {
  const { t, i18n } = useTranslation();
  return (
    <section aria-label={t("mail.attachments")} className="mt-3">
      <ul className="flex flex-wrap gap-2">
        {attachments.map((attachment) => (
          <li key={attachment.id}>
            <a
              href={attachmentUrl(messageId, attachment.id)}
              download={attachment.filename ?? true}
              className="flex max-w-64 items-center gap-2 rounded-md border px-2.5 py-1.5 text-ui outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              <Paperclip aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">
                {attachment.filename || t("mail.unnamedAttachment")}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatSize(attachment.size, i18n.language)}
              </span>
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
