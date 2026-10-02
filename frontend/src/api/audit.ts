import { infiniteQueryOptions } from "@tanstack/react-query";

import { API_BASE_PATH, api, unwrap } from "./client";
import type { components, operations } from "./schema.gen";

export type AuditEvent = components["schemas"]["AuditEventRead"];
export type AuditAction = components["schemas"]["AuditAction"];
type AuditQuery = NonNullable<operations["audit_get_events"]["parameters"]["query"]>;

/** Event types grouped for the filter; must list every backend `AuditAction`. */
const actionGroups = {
  "auth.setup_completed": "auth",
  "auth.login_succeeded": "auth",
  "auth.login_failed": "auth",
  "auth.logout": "auth",
  "auth.session_revoked": "auth",
  "user.created": "users",
  "user.role_changed": "users",
  "user.deactivated": "users",
  "user.reactivated": "users",
  "user.invited": "users",
  "user.password_set": "users",
  "user.deleted": "users",
  "user.updated": "users",
  "group.created": "users",
  "group.updated": "users",
  "group.deleted": "users",
  "group.member_added": "users",
  "group.member_removed": "users",
  "idp.config_changed": "settings",
  "ai.settings_changed": "settings",
  "mailbox.created": "mailboxes",
  "mailbox.shared": "mailboxes",
  "mailbox.unshared": "mailboxes",
  "mailbox.deleted": "mailboxes",
  "mail.sent": "mailboxes",
  "data.exported": "data",
  "data.deleted": "data",
  "data.retention_changed": "data",
  "todo_export.changed": "data",
  "crypto.keys_rotated": "operations",
  "audit.exported": "operations",
} as const satisfies Record<AuditAction, string>;

export type AuditActionGroup = (typeof actionGroups)[AuditAction];

export const auditActionGroups = Object.entries(actionGroups).reduce<
  { group: AuditActionGroup; actions: AuditAction[] }[]
>((groups, [action, group]) => {
  const existing = groups.find((entry) => entry.group === group);
  if (existing) existing.actions.push(action as AuditAction);
  else groups.push({ group, actions: [action as AuditAction] });
  return groups;
}, []);

export function isAuditAction(value: unknown): value is AuditAction {
  return typeof value === "string" && Object.hasOwn(actionGroups, value);
}

/** Filters as kept in the URL: dates are local calendar days (`YYYY-MM-DD`), both inclusive. */
export interface AuditFilters {
  action?: AuditAction;
  from?: string;
  to?: string;
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isDate(value: unknown): value is string {
  return typeof value === "string" && DATE.test(value) && !Number.isNaN(Date.parse(value));
}

function startOfDay(date: string, offsetDays = 0) {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year ?? 1970, (month ?? 1) - 1, (day ?? 1) + offsetDays).toISOString();
}

export function toAuditQuery({ action, from, to }: AuditFilters): AuditQuery {
  return {
    ...(action && { action }),
    ...(from && { since: startOfDay(from) }),
    ...(to && { until: startOfDay(to, 1) }),
  };
}

const PAGE_SIZE = 50;

export function auditEventsQueryOptions(filters: AuditFilters) {
  return infiniteQueryOptions({
    queryKey: ["audit", "events", filters],
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/audit/events", {
          params: { query: { ...toAuditQuery(filters), limit: PAGE_SIZE, before: pageParam } },
          signal,
        }),
      ),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_before ?? undefined,
    meta: { errorToast: false },
  });
}

/** Download link for the CSV export (same origin, authenticated by the session cookie). */
export function auditExportUrl(filters: AuditFilters) {
  const params = new URLSearchParams(
    Object.entries(toAuditQuery(filters)).map(([key, value]) => [key, String(value)]),
  );
  const query = params.size ? `?${params}` : "";
  return `${API_BASE_PATH}/audit/events/export${query}`;
}
