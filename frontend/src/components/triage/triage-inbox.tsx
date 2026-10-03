import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { MessageSummary } from "@/api/mail";
import { type TriagedMessage, triageInboxQueryOptions } from "@/api/triage";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { useCategories, useCategoryName } from "./use-triage";

/** `category` search param of the inbox: grouped (`all`), one category ID or `none`. */
export const GROUPED = "all";
export const UNCATEGORIZED = "none";

const CATEGORY_PARAM = /^(all|none|[0-9a-f-]{36})$/i;

export function parseCategoryParam(value: unknown) {
  return typeof value === "string" && CATEGORY_PARAM.test(value) ? value : undefined;
}

interface TriageInboxOptions {
  mailbox?: string;
  unread?: boolean;
  /** `category` search param; the inbox is ordered by date without it. */
  category?: string;
}

/**
 * The inbox ordered by category and priority (`GET /triage/inbox/messages`) and the headings
 * between the groups. Disabled (and `enabled: false`) without a category param.
 */
export function useTriageInbox({ mailbox, unread, category }: TriageInboxOptions) {
  const { t, i18n } = useTranslation();
  const categoryName = useCategoryName();
  const categories = useCategories();
  const enabled = !!category;
  const query = useInfiniteQuery({
    ...triageInboxQueryOptions({ mailbox, unread, category: category ?? GROUPED }),
    enabled,
  });
  const items = useMemo<TriagedMessage[]>(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  );
  // Counts come with the first page only.
  const groups = query.data?.pages[0]?.groups ?? undefined;
  const total = query.data?.pages[0]?.total ?? 0;

  const groupName = useCallback(
    (categoryId: string | null) => {
      const found = categoryId ? categories.data?.find((item) => item.id === categoryId) : null;
      return found ? categoryName(found) : t("triage.uncategorized");
    },
    [categories.data, categoryName, t],
  );

  // Headings only when all groups are listed.
  const groupHeader = useMemo(() => {
    if (category !== GROUPED) return undefined;
    const totals = new Map(groups?.map((group) => [group.category_id, group.total]));
    const format = new Intl.NumberFormat(i18n.language);
    return (message: MessageSummary, previous: MessageSummary | undefined): ReactNode => {
      const id = (message as TriagedMessage).category_id ?? null;
      if (previous && ((previous as TriagedMessage).category_id ?? null) === id) return null;
      const count = totals.get(id) ?? 0;
      return (
        <GroupHeader
          name={groupName(id)}
          param={id ?? UNCATEGORIZED}
          count={t("triage.view.groupCount", { count, formatted: format.format(count) })}
        />
      );
    };
  }, [category, groups, groupName, i18n.language, t]);

  return { enabled, query, items, total, groups, groupName, groupHeader };
}

function GroupHeader({ name, param, count }: { name: string; param: string; count: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex h-full items-end gap-2 border-b border-border/60 bg-background px-4 pb-1.5">
      <h2 className="text-xs font-medium text-foreground">
        <Link
          to="/inbox"
          search={(previous) => ({ ...previous, category: param, message: undefined })}
          title={t("triage.view.showOnly", { category: name })}
          className="rounded-sm outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/80"
        >
          {name}
        </Link>
      </h2>
      <span className="text-xs text-muted-foreground tabular-nums">{count}</span>
    </div>
  );
}

/**
 * Filter-bar select: inbox by date, grouped by category, or a single category (with counts
 * once the grouped data is loaded).
 */
export function TriageViewSelect({
  value,
  groups,
  onChange,
}: {
  value: string | undefined;
  groups: { category_id: string | null; total: number }[] | undefined;
  onChange: (value: string | undefined) => void;
}) {
  const { t, i18n } = useTranslation();
  const categoryName = useCategoryName();
  const { visible } = useCategories();
  const totals = new Map(groups?.map((group) => [group.category_id, group.total]));
  const format = new Intl.NumberFormat(i18n.language);
  const option = (name: string, id: string | null) => {
    const count = totals.get(id);
    return count === undefined
      ? t("triage.view.only", { category: name })
      : t("triage.view.onlyCount", { category: name, formatted: format.format(count) });
  };

  return (
    <NativeSelect
      size="sm"
      className="w-40 sm:w-48"
      aria-label={t("triage.view.label")}
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value || undefined)}
    >
      <NativeSelectOption value="">{t("triage.view.byDate")}</NativeSelectOption>
      <NativeSelectOption value={GROUPED}>{t("triage.view.byCategory")}</NativeSelectOption>
      {visible.map((category) => (
        <NativeSelectOption key={category.id} value={category.id}>
          {option(categoryName(category), category.id)}
        </NativeSelectOption>
      ))}
      <NativeSelectOption value={UNCATEGORIZED}>
        {option(t("triage.uncategorized"), null)}
      </NativeSelectOption>
    </NativeSelect>
  );
}
