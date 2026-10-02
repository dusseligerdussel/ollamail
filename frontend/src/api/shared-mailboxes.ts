import { queryOptions } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

type Schemas = components["schemas"];

export type SharedMailbox = Schemas["SharedMailboxRead"];
export type SharedMailboxCreate = Schemas["SharedMailboxCreate"];
export type MailboxAssignment = Schemas["MailboxAssignmentRead"];
export type MailboxAssignmentsUpdate = Schemas["MailboxAssignmentsUpdate"];
export type GroupAssignment = Schemas["GroupAssignment"];
export type MailboxMember = Schemas["MailboxMember"];

/**
 * Admin keys start with `mailbox`, so `mailbox.*` server events refresh them too (see
 * `use-events.ts`).
 */
export const sharedMailboxKeys = {
  all: ["mailbox", "shared"] as const,
  detail: (mailboxId: string) => ["mailbox", "shared", mailboxId] as const,
  members: (mailboxId: string) => ["mailbox", mailboxId, "members"] as const,
};

export const sharedMailboxesQueryOptions = queryOptions({
  queryKey: sharedMailboxKeys.all,
  queryFn: ({ signal }) => unwrap(api.GET("/admin/shared-mailboxes", { signal })),
  meta: { errorToast: false },
});

export function sharedMailboxQueryOptions(mailboxId: string) {
  return queryOptions({
    queryKey: sharedMailboxKeys.detail(mailboxId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/admin/shared-mailboxes/{mailbox_id}", {
          params: { path: { mailbox_id: mailboxId } },
          signal,
        }),
      ),
    meta: { errorToast: false },
  });
}

/** Everybody who may read a mailbox (to assign team todos to). */
export function mailboxMembersQueryOptions(mailboxId: string) {
  return queryOptions({
    queryKey: sharedMailboxKeys.members(mailboxId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/mailboxes/{mailbox_id}/members", {
          params: { path: { mailbox_id: mailboxId } },
          signal,
        }),
      ),
  });
}

export function createSharedMailbox(body: SharedMailboxCreate) {
  return unwrap(api.POST("/admin/shared-mailboxes", { body }));
}

export function updateSharedMailbox(mailboxId: string, body: Schemas["MailboxUpdate"]) {
  return unwrap(
    api.PATCH("/admin/shared-mailboxes/{mailbox_id}", {
      params: { path: { mailbox_id: mailboxId } },
      body,
    }),
  );
}

export function setAssignments(mailboxId: string, body: MailboxAssignmentsUpdate) {
  return unwrap(
    api.PUT("/admin/shared-mailboxes/{mailbox_id}/assignments", {
      params: { path: { mailbox_id: mailboxId } },
      body,
    }),
  );
}

export function deleteSharedMailbox(mailboxId: string) {
  return unwrap(
    api.DELETE("/admin/shared-mailboxes/{mailbox_id}", {
      params: { path: { mailbox_id: mailboxId } },
    }),
  );
}

export function syncSharedMailbox(mailboxId: string) {
  return unwrap(
    api.POST("/admin/shared-mailboxes/{mailbox_id}/sync", {
      params: { path: { mailbox_id: mailboxId } },
    }),
  );
}
