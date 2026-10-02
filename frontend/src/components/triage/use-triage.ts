import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import {
  builtinCategoryKeys,
  type Category,
  categoriesQueryOptions,
  triageQueryOptions,
} from "@/api/triage";

type BuiltinKey = (typeof builtinCategoryKeys)[number];

function isBuiltin(key: string | null | undefined): key is BuiltinKey {
  return builtinCategoryKeys.includes(key as BuiltinKey);
}

/** Display name of a category: translated for built-in ones, as entered otherwise. */
export function useCategoryName() {
  const { t } = useTranslation();
  return useCallback(
    (category: Pick<Category, "name" | "builtin_key">) =>
      isBuiltin(category.builtin_key) ? t(`triage.builtin.${category.builtin_key}`) : category.name,
    [t],
  );
}

/** The user's categories in their order; `visible` are those that can be assigned. */
export function useCategories() {
  const query = useQuery(categoriesQueryOptions);
  const visible = useMemo(
    () => query.data?.filter((category) => !category.hidden) ?? [],
    [query.data],
  );
  return { ...query, visible };
}

/**
 * Triage result of a message with its category (`undefined` while loading or untriaged), and
 * the categories query. Pass `categories` on to child components instead of calling
 * `useCategories()` in children that mount conditionally: a component mounting while the query
 * failed starts a new request, and if that request hides it again (it is pending meanwhile),
 * the two keep each other going (#86).
 */
export function useMessageTriage(messageId: string | undefined) {
  const triage = useQuery({ ...triageQueryOptions(messageId ?? ""), enabled: !!messageId });
  const categories = useCategories();
  const category = categories.data?.find((item) => item.id === triage.data?.category_id);
  return {
    triage: triage.data ?? undefined,
    category,
    categories,
    isPending: triage.isPending || categories.isPending,
  };
}
