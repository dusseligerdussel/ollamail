import { useQuery } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { createContext, type ReactNode, useContext, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { type Category, triageQueryOptions } from "@/api/triage";
import { cn } from "@/lib/utils";

import { useCategories, useCategoryName } from "./use-triage";

/** What all labels of a list share, loaded once instead of in every row (#111). */
interface LabelContext {
  hidden: boolean;
  categories: Map<string, Category>;
  categoryName: (category: Category) => string;
  t: TFunction;
}

const LabelContext = createContext<LabelContext | null>(null);

function useLabelContext(hidden: boolean): LabelContext {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const { data } = useCategories();
  const categories = useMemo(
    () => new Map((data ?? []).map((category) => [category.id, category])),
    [data],
  );
  return useMemo(
    () => ({ hidden, categories, categoryName, t }),
    [hidden, categories, categoryName, t],
  );
}

/**
 * Wraps a list with labels: shares the categories with them and hides them if `hidden`, e.g. in
 * the inbox grouped by category, where they repeat the group.
 */
export function HideTriageLabels({ hidden, children }: { hidden: boolean; children: ReactNode }) {
  return <LabelContext value={useLabelContext(hidden)}>{children}</LabelContext>;
}

/**
 * The category of a message as a quiet label in a list row. Nothing while the message is not
 * triaged yet or its category is hidden; high priority is set slightly stronger.
 */
export function TriageLabel({ messageId }: { messageId: string }) {
  const shared = useContext(LabelContext);
  if (shared?.hidden) return null;
  return shared ? (
    <Label messageId={messageId} context={shared} />
  ) : (
    <StandaloneLabel messageId={messageId} />
  );
}

/** Outside of `HideTriageLabels`, a label loads the categories itself. */
function StandaloneLabel({ messageId }: { messageId: string }) {
  return <Label messageId={messageId} context={useLabelContext(false)} />;
}

function Label({ messageId, context }: { messageId: string; context: LabelContext }) {
  const { t, categories, categoryName } = context;
  const { data: triage } = useQuery(triageQueryOptions(messageId));
  const category = triage?.category_id ? categories.get(triage.category_id) : undefined;
  if (!triage || !category || category.hidden) return null;
  const name = categoryName(category);
  const high = triage.priority === 1;
  return (
    <span
      data-testid="triage-label"
      data-priority={triage.priority}
      title={t(high ? "triage.labelHighPriority" : "triage.label", { category: name })}
      className={cn(
        "max-w-32 shrink-0 truncate rounded-sm border px-1.5 text-xs leading-[1.125rem]",
        high ? "border-foreground/25 font-medium text-foreground" : "text-muted-foreground",
      )}
    >
      <span className="sr-only">
        {t(high ? "triage.labelHighPriority" : "triage.label", { category: name })}
      </span>
      <span aria-hidden="true">{name}</span>
    </span>
  );
}
