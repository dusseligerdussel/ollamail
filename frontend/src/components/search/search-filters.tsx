import { useQuery } from "@tanstack/react-query";
import { ChevronDown, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { mailboxesQueryOptions } from "@/api/mail";
import { categoriesQueryOptions, type SearchFilterParams } from "@/api/search";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export const periods = ["7d", "30d", "1y"] as const;
export type Period = (typeof periods)[number];

/** Filters as chosen in the chips; turned into API filters by `toFilterParams`. */
export interface SearchFilterState {
  period?: Period;
  sender?: string;
  mailbox?: string;
  category?: string;
}

const PERIOD_DAYS: Record<Period, number> = { "7d": 7, "30d": 30, "1y": 365 };

/** API filters; the period starts at midnight (UTC), so the value is stable for a day. */
export function toFilterParams(filters: SearchFilterState, now = new Date()): SearchFilterParams {
  const params: SearchFilterParams = {};
  if (filters.period) {
    const since = new Date(
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) -
        PERIOD_DAYS[filters.period] * 86_400_000,
    );
    params.since = since.toISOString();
  }
  if (filters.sender) params.sender = filters.sender;
  if (filters.mailbox) params.mailbox_ids = [filters.mailbox];
  if (filters.category) params.category_ids = [filters.category];
  return params;
}

export function hasFilters(filters: SearchFilterState) {
  return Object.values(filters).some(Boolean);
}

interface ChipProps {
  label: string;
  /** Selected value; the chip is active when set. */
  value?: string;
  onClear: () => void;
  children: ReactNode;
}

/** A filter chip: opens its options; when active it shows the value and a clear button. */
function Chip({ label, value, onClear, children }: ChipProps) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "flex h-7 shrink-0 items-center rounded-full border text-xs",
        value ? "border-brand/50 bg-brand/10" : "bg-background",
      )}
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={cn(
              "flex h-full max-w-56 items-center gap-1 rounded-full pr-2 pl-3 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              !value && "text-muted-foreground hover:text-foreground",
            )}
          >
            <span className="truncate">{value ? `${label}: ${value}` : label}</span>
            {!value && <ChevronDown aria-hidden className="size-3" />}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 min-w-44 overflow-y-auto">
          {children}
        </DropdownMenuContent>
      </DropdownMenu>
      {value && (
        <button
          type="button"
          onClick={onClear}
          aria-label={t("search.filters.clear", { filter: label })}
          className="mr-1 flex size-5 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <X aria-hidden className="size-3" />
        </button>
      )}
    </div>
  );
}

/** The sender chip edits its value inline (a free-text filter). */
function SenderChip({
  value,
  onChange,
}: {
  value?: string;
  onChange: (sender: string | undefined) => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const input = useRef<HTMLInputElement>(null);
  const label = t("search.filters.sender");

  useEffect(() => {
    if (editing) input.current?.focus();
  }, [editing]);

  const apply = () => {
    onChange(draft.trim() || undefined);
    setEditing(false);
  };

  if (editing) {
    return (
      // No nested <form>: the chips sit inside the search form.
      <div className="flex h-7 shrink-0 items-center">
        <Input
          ref={input}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              event.stopPropagation();
              apply();
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setEditing(false);
            }
          }}
          onBlur={apply}
          aria-label={label}
          placeholder={t("search.filters.senderPlaceholder")}
          maxLength={200}
          enterKeyHint="done"
          className="h-7 w-48 rounded-full px-3 text-xs md:text-xs"
        />
      </div>
    );
  }

  return (
    <div
      className={cn(
        "flex h-7 shrink-0 items-center rounded-full border text-xs",
        value ? "border-brand/50 bg-brand/10" : "bg-background",
      )}
    >
      <button
        type="button"
        onClick={() => {
          setDraft(value ?? "");
          setEditing(true);
        }}
        className={cn(
          "flex h-full max-w-56 items-center rounded-full px-3 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
          !value && "text-muted-foreground hover:text-foreground",
          value && "pr-2",
        )}
      >
        <span className="truncate">{value ? `${label}: ${value}` : label}</span>
      </button>
      {value && (
        <button
          type="button"
          onClick={() => onChange(undefined)}
          aria-label={t("search.filters.clear", { filter: label })}
          className="mr-1 flex size-5 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-accent hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <X aria-hidden className="size-3" />
        </button>
      )}
    </div>
  );
}

interface SearchFiltersProps {
  filters: SearchFilterState;
  onChange: (filters: SearchFilterState) => void;
}

/** Filter chips: period, sender, mailbox, category. */
export function SearchFilters({ filters, onChange }: SearchFiltersProps) {
  const { t } = useTranslation();
  const mailboxes = useQuery(mailboxesQueryOptions);
  const categories = useQuery(categoriesQueryOptions);
  const set = (change: Partial<SearchFilterState>) => {
    const next = { ...filters, ...change };
    for (const key of Object.keys(next) as (keyof SearchFilterState)[]) {
      if (!next[key]) delete next[key];
    }
    onChange(next);
  };

  const mailboxName = mailboxes.data?.find(
    (mailbox) => mailbox.id === filters.mailbox,
  )?.display_name;
  const visibleCategories = categories.data?.filter((category) => !category.hidden) ?? [];
  const categoryName = (id: string) => categories.data?.find((item) => item.id === id)?.name;

  return (
    <fieldset className="flex min-w-0 flex-wrap items-center gap-1.5">
      <legend className="sr-only">{t("search.filters.label")}</legend>
      <Chip
        label={t("search.filters.period")}
        value={filters.period && t(`search.filters.periods.${filters.period}`)}
        onClear={() => set({ period: undefined })}
      >
        <DropdownMenuRadioGroup
          value={filters.period ?? ""}
          onValueChange={(value) => set({ period: (value || undefined) as Period | undefined })}
        >
          <DropdownMenuRadioItem value="">{t("search.filters.anyTime")}</DropdownMenuRadioItem>
          {periods.map((period) => (
            <DropdownMenuRadioItem key={period} value={period}>
              {t(`search.filters.periods.${period}`)}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </Chip>
      <SenderChip value={filters.sender} onChange={(sender) => set({ sender })} />
      {(mailboxes.data?.length ?? 0) > 1 && (
        <Chip
          label={t("search.filters.mailbox")}
          value={mailboxName}
          onClear={() => set({ mailbox: undefined })}
        >
          <DropdownMenuRadioGroup
            value={filters.mailbox ?? ""}
            onValueChange={(value) => set({ mailbox: value || undefined })}
          >
            <DropdownMenuRadioItem value="">{t("mail.allMailboxes")}</DropdownMenuRadioItem>
            {mailboxes.data?.map((mailbox) => (
              <DropdownMenuRadioItem key={mailbox.id} value={mailbox.id}>
                {mailbox.display_name}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </Chip>
      )}
      {visibleCategories.length > 0 && (
        <Chip
          label={t("search.filters.category")}
          value={filters.category && categoryName(filters.category)}
          onClear={() => set({ category: undefined })}
        >
          <DropdownMenuRadioGroup
            value={filters.category ?? ""}
            onValueChange={(value) => set({ category: value || undefined })}
          >
            <DropdownMenuRadioItem value="">
              {t("search.filters.anyCategory")}
            </DropdownMenuRadioItem>
            {visibleCategories.map((category) => (
              <DropdownMenuRadioItem key={category.id} value={category.id}>
                {categoryName(category.id)}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </Chip>
      )}
      {hasFilters(filters) && (
        <Button
          variant="ghost"
          size="xs"
          className="text-muted-foreground"
          onClick={() => onChange({})}
        >
          {t("search.filters.reset")}
        </Button>
      )}
    </fieldset>
  );
}
