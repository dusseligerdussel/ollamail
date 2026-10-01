import { useInfiniteQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, Download, ScrollText } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import {
  type AuditEvent,
  type AuditFilters,
  auditActionGroups,
  auditEventsQueryOptions,
  auditExportUrl,
  isAuditAction,
  isDate,
} from "@/api/audit";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { NotFound } from "@/components/not-found";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOptGroup,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { useCurrentUser } from "@/hooks/use-current-user";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

export const Route = createFileRoute("/admin_/audit")({
  validateSearch: (search: Record<string, unknown>): AuditFilters => ({
    ...(isAuditAction(search.action) && { action: search.action }),
    ...(isDate(search.from) && { from: search.from }),
    ...(isDate(search.to) && { to: search.to }),
  }),
  component: AuditLogPage,
});

function AuditLogPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const filters = Route.useSearch();
  if (!isAdmin) return <NotFound />;

  return (
    <>
      <PageHeader
        title={t("pages.audit.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/admin" aria-label={t("pages.audit.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
        actions={
          <Button asChild size="sm" variant="outline">
            <a href={auditExportUrl(filters)} download>
              <Download />
              {t("pages.audit.export")}
            </a>
          </Button>
        }
      />
      <FilterBar filters={filters} />
      <div className="flex-1 overflow-y-auto">
        <AuditEvents filters={filters} />
      </div>
    </>
  );
}

function FilterBar({ filters }: { filters: AuditFilters }) {
  const { t } = useTranslation();
  const navigate = Route.useNavigate();
  const id = useId();
  const active = Boolean(filters.action || filters.from || filters.to);

  const update = (changes: Partial<AuditFilters>) =>
    navigate({
      search: (previous) => {
        const next: AuditFilters = { ...previous, ...changes };
        for (const key of Object.keys(next) as (keyof AuditFilters)[]) {
          if (!next[key]) delete next[key];
        }
        return next;
      },
      replace: true,
    });

  const labelClass = "mb-1 block text-xs font-medium text-muted-foreground";
  return (
    <search
      aria-label={t("pages.audit.filters")}
      className="grid grid-cols-2 items-end gap-3 border-b px-4 py-3 sm:flex sm:flex-wrap md:px-5"
    >
      <div className="col-span-2 sm:w-64">
        <label htmlFor={`${id}-action`} className={labelClass}>
          {t("pages.audit.action")}
        </label>
        <NativeSelect
          id={`${id}-action`}
          size="sm"
          className="w-full"
          value={filters.action ?? ""}
          onChange={(event) =>
            update({ action: isAuditAction(event.target.value) ? event.target.value : undefined })
          }
        >
          <NativeSelectOption value="">{t("pages.audit.allActions")}</NativeSelectOption>
          {auditActionGroups.map(({ group, actions }) => (
            <NativeSelectOptGroup key={group} label={t(`pages.audit.groups.${group}`)}>
              {actions.map((action) => (
                <NativeSelectOption key={action} value={action}>
                  {t(`pages.audit.actions.${action}`)}
                </NativeSelectOption>
              ))}
            </NativeSelectOptGroup>
          ))}
        </NativeSelect>
      </div>
      <div className="sm:w-40">
        <label htmlFor={`${id}-from`} className={labelClass}>
          {t("pages.audit.from")}
        </label>
        <Input
          id={`${id}-from`}
          type="date"
          className="h-8"
          value={filters.from ?? ""}
          max={filters.to}
          onChange={(event) => update({ from: event.target.value || undefined })}
        />
      </div>
      <div className="sm:w-40">
        <label htmlFor={`${id}-to`} className={labelClass}>
          {t("pages.audit.to")}
        </label>
        <Input
          id={`${id}-to`}
          type="date"
          className="h-8"
          value={filters.to ?? ""}
          min={filters.from}
          onChange={(event) => update({ to: event.target.value || undefined })}
        />
      </div>
      {active && (
        <Button
          size="sm"
          variant="ghost"
          className="col-span-2 justify-self-start"
          onClick={() => update({ action: undefined, from: undefined, to: undefined })}
        >
          {t("pages.audit.reset")}
        </Button>
      )}
    </search>
  );
}

function AuditEvents({ filters }: { filters: AuditFilters }) {
  const { t } = useTranslation();
  const wide = useMediaQuery(mediaQueries.sidebar);
  const query = useInfiniteQuery(auditEventsQueryOptions(filters));

  if (query.isPending) return <ListSkeleton />;
  // A failed "load more" keeps the loaded pages (and data); only a failed first page has none.
  if (!query.data) return <InlineError error={query.error} className="px-4 py-4 md:px-5" />;

  const events = query.data.pages.flatMap((page) => page.items);
  if (events.length === 0) {
    const filtered = Boolean(filters.action || filters.from || filters.to);
    return (
      <EmptyState
        icon={ScrollText}
        title={t("pages.audit.emptyTitle")}
        description={t(
          filtered ? "pages.audit.emptyFilteredDescription" : "pages.audit.emptyDescription",
        )}
      />
    );
  }

  return (
    <>
      {wide ? <EventTable events={events} /> : <EventList events={events} />}
      {query.hasNextPage && (
        <div className="flex justify-center px-4 py-4">
          <Button
            size="sm"
            variant="outline"
            disabled={query.isFetchingNextPage}
            onClick={() => query.fetchNextPage()}
          >
            {t(query.isFetchingNextPage ? "pages.audit.loadingMore" : "pages.audit.loadMore")}
          </Button>
        </div>
      )}
      {query.isFetchNextPageError && (
        <InlineError error={query.error} className="px-4 pb-4 md:px-5" />
      )}
    </>
  );
}

function useEventText() {
  const { t, i18n } = useTranslation();
  const time = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "medium",
  });

  return {
    time: (event: AuditEvent) => time.format(new Date(event.occurred_at)),
    action: (event: AuditEvent) => t(`pages.audit.actions.${event.action}`),
    actor: (event: AuditEvent) => {
      if (event.actor_kind === "system") return t("pages.audit.actor.system");
      if (event.actor_kind === "anonymous") return t("pages.audit.actor.anonymous");
      return event.actor_name ?? t("pages.audit.actor.deleted");
    },
    target: (event: AuditEvent) => {
      if (!event.target_type) return "";
      if (event.target_name) return event.target_name;
      const type = t(`pages.audit.targets.${event.target_type}`);
      // IDs are UUIDs: the first block is enough to tell entries apart in the list.
      return event.target_id ? `${type} ${event.target_id.slice(0, 8)}` : type;
    },
    details: (event: AuditEvent) =>
      Object.entries(event.details)
        .map(([key, value]) => `${key}: ${value === null ? "–" : String(value)}`)
        .join(" · "),
  };
}

function EventTable({ events }: { events: AuditEvent[] }) {
  const { t } = useTranslation();
  const text = useEventText();
  const headClass = "h-8 px-2 text-left text-xs font-medium text-muted-foreground first:pl-5";
  const cellClass = "h-row px-2 first:pl-5 last:pr-5";

  return (
    <table
      aria-label={t("pages.audit.title")}
      className="w-full table-fixed border-collapse text-ui"
    >
      <thead className="sticky top-0 bg-background">
        <tr className="border-b">
          <th scope="col" className={`${headClass} w-48`}>
            {t("pages.audit.columns.time")}
          </th>
          <th scope="col" className={`${headClass} w-56`}>
            {t("pages.audit.columns.event")}
          </th>
          <th scope="col" className={`${headClass} w-44`}>
            {t("pages.audit.columns.actor")}
          </th>
          <th scope="col" className={`${headClass} w-44`}>
            {t("pages.audit.columns.target")}
          </th>
          <th scope="col" className={headClass}>
            {t("pages.audit.columns.details")}
          </th>
        </tr>
      </thead>
      <tbody>
        {events.map((event) => (
          <tr key={event.id} className="border-b border-border/60">
            <td className={`${cellClass} whitespace-nowrap text-muted-foreground tabular-nums`}>
              <time dateTime={event.occurred_at}>{text.time(event)}</time>
            </td>
            <td className={`${cellClass} truncate font-medium`}>{text.action(event)}</td>
            <td className={`${cellClass} truncate`}>{text.actor(event)}</td>
            <td className={`${cellClass} truncate`}>{text.target(event)}</td>
            <td className={`${cellClass} truncate text-muted-foreground`}>{text.details(event)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EventList({ events }: { events: AuditEvent[] }) {
  const { t } = useTranslation();
  const text = useEventText();

  return (
    <ul aria-label={t("pages.audit.title")} className="flex flex-col">
      {events.map((event) => {
        const target = text.target(event);
        const details = text.details(event);
        return (
          <li key={event.id} className="border-b border-border/60 px-4 py-2.5 text-ui">
            <div className="flex items-baseline justify-between gap-3">
              <span className="truncate font-medium">{text.action(event)}</span>
              <time
                dateTime={event.occurred_at}
                className="shrink-0 text-xs text-muted-foreground tabular-nums"
              >
                {text.time(event)}
              </time>
            </div>
            <div className="truncate text-muted-foreground">
              {target ? `${text.actor(event)} → ${target}` : text.actor(event)}
            </div>
            {details && <div className="truncate text-xs text-muted-foreground">{details}</div>}
          </li>
        );
      })}
    </ul>
  );
}
