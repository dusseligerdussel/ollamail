import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { API_BASE_PATH, api, unwrap } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

export type Mailbox = components["schemas"]["MailboxRead"];
export type MailboxType = components["schemas"]["MailboxType"];
export type MailboxSyncStatus = components["schemas"]["MailboxSyncStatus"];
export type MailboxCreate = components["schemas"]["MailboxCreate"];
export type MailboxConnection = components["schemas"]["MailboxConnection"];
export type MailboxUpdate = components["schemas"]["MailboxUpdate"];
export type MailboxProvider = components["schemas"]["MailboxProviderRead"];
export type ConnectionTestResult = components["schemas"]["ConnectionTestResult"];
export type AutodiscoverSuggestion = components["schemas"]["AutodiscoverSuggestion"];
export type Folder = components["schemas"]["FolderRead"];
export type FolderRole = components["schemas"]["FolderRole"];
export type MessageSummary = components["schemas"]["MessageSummary"];
export type MessagePage = components["schemas"]["MessagePage"];
export type MessageDetail = components["schemas"]["MessageDetail"];
export type MessageBody = components["schemas"]["MessageBody"];
export type Thread = components["schemas"]["ThreadRead"];
export type Attachment = components["schemas"]["AttachmentRead"];
export type Address = components["schemas"]["AddressRead"];
export type MessageAction = components["schemas"]["MessageAction"];
export type MessageActionResult = components["schemas"]["MessageActionResult"];

/**
 * Query keys. Server events invalidate by their resource (`mailbox.*` → `["mailbox"]`,
 * `message.*` → `["message"]`, see `use-events.ts`), so every key starts with one of them.
 */
export const mailKeys = {
  mailboxes: ["mailbox", "list"] as const,
  providers: ["mailbox", "providers"] as const,
  folders: (mailboxId: string) => ["mailbox", mailboxId, "folders"] as const,
  messages: (filters: MessageFilters) => ["message", "list", filters] as const,
  thread: (messageId: string) => ["message", "thread", messageId] as const,
  body: (messageId: string) => ["message", "body", messageId, "external"] as const,
};

function fetchMailboxes({ signal }: { signal: AbortSignal }) {
  return unwrap(api.GET("/mailboxes", { signal }));
}

/** The mailboxes to read mail from: without those being removed (status `deleting`). */
export const mailboxesQueryOptions = queryOptions({
  queryKey: mailKeys.mailboxes,
  queryFn: fetchMailboxes,
  select: (mailboxes) => mailboxes.filter((mailbox) => mailbox.status.phase !== "deleting"),
});

/** All mailboxes, including those being removed in the background (mailbox settings). */
export const allMailboxesQueryOptions = queryOptions({
  queryKey: mailKeys.mailboxes,
  queryFn: fetchMailboxes,
});

export const mailboxProvidersQueryOptions = queryOptions({
  queryKey: mailKeys.providers,
  queryFn: ({ signal }) => unwrap(api.GET("/mailboxes/providers", { signal })),
  staleTime: 5 * 60_000,
});

export function foldersQueryOptions(mailboxId: string) {
  return queryOptions({
    queryKey: mailKeys.folders(mailboxId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/mailboxes/{mailbox_id}/folders", {
          params: { path: { mailbox_id: mailboxId } },
          signal,
        }),
      ),
  });
}

/** Inbox filters as kept in the URL. Without `folder`, the inbox folders are listed. */
export interface MessageFilters {
  mailbox?: string;
  folder?: string;
  unread?: boolean;
}

export const MESSAGE_PAGE_SIZE = 100;

export function messagesQueryOptions(filters: MessageFilters) {
  return infiniteQueryOptions({
    queryKey: mailKeys.messages(filters),
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/messages", {
          params: {
            query: {
              mailbox_id: filters.mailbox,
              folder_id: filters.folder,
              unread: filters.unread,
              cursor: pageParam,
              limit: MESSAGE_PAGE_SIZE,
            },
          },
          signal,
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}

export function threadQueryOptions(messageId: string) {
  return queryOptions({
    queryKey: mailKeys.thread(messageId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/messages/{message_id}/thread", {
          params: { path: { message_id: messageId } },
          signal,
        }),
      ),
  });
}

/** The body with external images, loaded only after the user asked for them. */
export function bodyWithImagesQueryOptions(messageId: string) {
  return queryOptions({
    queryKey: mailKeys.body(messageId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/messages/{message_id}/body", {
          params: { path: { message_id: messageId }, query: { external_images: true } },
          signal,
        }),
      ),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function attachmentUrl(messageId: string, attachmentId: string) {
  return `${API_BASE_PATH}/messages/${encodeURIComponent(messageId)}/attachments/${encodeURIComponent(attachmentId)}`;
}

export function setSeen(messageId: string, seen: boolean) {
  return unwrap(
    api.PATCH("/messages/{message_id}", {
      params: { path: { message_id: messageId } },
      body: { seen },
    }),
  );
}

export function setFlagged(messageId: string, flagged: boolean) {
  return unwrap(
    api.PATCH("/messages/{message_id}", {
      params: { path: { message_id: messageId } },
      body: { flagged },
    }),
  );
}

/** Archive, trash or move a message on the mail server (#148). */
export function runMessageAction(messageId: string, action: MessageAction, folderId?: string) {
  return unwrap(
    api.POST("/messages/{message_id}/actions", {
      params: { path: { message_id: messageId } },
      body: { action, folder_id: folderId ?? null },
    }),
  );
}

export function autodiscover(address: string) {
  return unwrap(api.POST("/mailboxes/autodiscover", { body: { address } }));
}

export function testConnection(body: MailboxConnection) {
  return unwrap(api.POST("/mailboxes/test", { body }));
}

export function createMailbox(body: MailboxCreate) {
  return unwrap(api.POST("/mailboxes", { body }));
}

export function updateMailbox(mailboxId: string, body: MailboxUpdate) {
  return unwrap(
    api.PATCH("/mailboxes/{mailbox_id}", { params: { path: { mailbox_id: mailboxId } }, body }),
  );
}

export function deleteMailbox(mailboxId: string) {
  return unwrap(
    api.DELETE("/mailboxes/{mailbox_id}", { params: { path: { mailbox_id: mailboxId } } }),
  );
}

export function syncMailbox(mailboxId: string) {
  return unwrap(
    api.POST("/mailboxes/{mailbox_id}/sync", { params: { path: { mailbox_id: mailboxId } } }),
  );
}

export function selectFolders(mailboxId: string, folders: { id: string; sync_enabled: boolean }[]) {
  return unwrap(
    api.PATCH("/mailboxes/{mailbox_id}/folders", {
      params: { path: { mailbox_id: mailboxId } },
      body: { folders },
    }),
  );
}

/**
 * Starts the OAuth connect flow of a provider (`oauth_start_path` from `/mailboxes/providers`).
 * Every flow answers `{ authorization_url }`; the browser then navigates there and comes back to
 * `returnTo` (or `/?mailbox_connected=…` for Gmail).
 */
export function startOAuthConnect(startPath: string, returnTo: string) {
  // The path comes from the server; each provider's start endpoint takes `return_to`
  // (providers that do not know it ignore it).
  return unwrap(
    api.POST(startPath as "/mail/gmail/oauth/start", {
      body: { return_to: returnTo } as components["schemas"]["GmailOAuthStart"],
    }),
  );
}

/** Machine-readable `error_code` of a failed request (e.g. `authentication_failed`). */
export function problemErrorCode(error: unknown): string | undefined {
  const code = isApiError(error) ? error.problem?.error_code : undefined;
  return typeof code === "string" ? code : undefined;
}
