import { useNavigate } from "@tanstack/react-router";
import { CalendarArrowDown, Rows3, Tag, Tags } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { type Category, type Triage, useCorrectTriage } from "@/api/triage";
import { useCommands } from "@/components/command-palette/command-provider";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import type { Command } from "@/lib/commands";

import { CategoryPicker } from "./category-picker";
import { useCategories, useCategoryName, useMessageTriage } from "./use-triage";

/** Opens the category picker for the opened or highlighted message. */
export const CHANGE_CATEGORY_KEYS = "c";

/** Corrects the category of a message, keeping its priority. */
export function useCorrectCategory(messageId: string | undefined, triage: Triage | undefined) {
  const { mutate } = useCorrectTriage();
  return useCallback(
    (category: Category) => {
      if (!messageId || category.id === triage?.category_id) return;
      mutate({ messageId, categoryId: category.id, priority: triage?.priority });
    },
    [messageId, triage, mutate],
  );
}

interface TriageControlsProps {
  /** The opened message, else the highlighted one in the list. */
  messageId: string | undefined;
  /** Inbox grouped or filtered by category (`category` search param), else by date. */
  byCategory: boolean;
  /** Switch between the views; `undefined` while not available (e.g. a folder is shown). */
  onViewChange?: (byCategory: boolean) => void;
}

/**
 * Keyboard and command palette access to triage in the inbox: `c` opens the category picker,
 * the palette offers "Categorize as …" per category and the grouped view.
 */
export function TriageControls({ messageId, byCategory, onViewChange }: TriageControlsProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const categoryName = useCategoryName();
  const { visible } = useCategories();
  const { triage } = useMessageTriage(messageId);
  const correct = useCorrectCategory(messageId, triage);
  const [pickerOpen, setPickerOpen] = useState(false);

  useShortcut(
    {
      id: "triage.change",
      keys: CHANGE_CATEGORY_KEYS,
      group: "list",
      description: t("triage.shortcuts.change"),
      enabled: !!messageId && visible.length > 0,
    },
    () => setPickerOpen(true),
  );

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [];
    if (messageId && visible.length > 0) {
      list.push({
        id: "triage.change",
        label: t("triage.commands.change"),
        group: "actions",
        icon: Tag,
        shortcut: CHANGE_CATEGORY_KEYS,
        run: () => setPickerOpen(true),
      });
      for (const category of visible) {
        list.push({
          id: `triage.assign.${category.id}`,
          label: t("triage.commands.assign", { category: categoryName(category) }),
          group: "actions",
          icon: Tag,
          keywords: [t("triage.change")],
          active: category.id === triage?.category_id,
          run: () => correct(category),
        });
      }
    }
    if (onViewChange) {
      list.push(
        byCategory
          ? {
              id: "triage.view",
              label: t("triage.commands.sortByDate"),
              group: "actions",
              icon: CalendarArrowDown,
              run: () => onViewChange(false),
            }
          : {
              id: "triage.view",
              label: t("triage.commands.groupByCategory"),
              group: "actions",
              icon: Rows3,
              run: () => onViewChange(true),
            },
      );
    }
    list.push({
      id: "triage.manage",
      label: t("triage.commands.manage"),
      group: "actions",
      icon: Tags,
      run: () => void navigate({ to: "/settings/categories" }),
    });
    return list;
  }, [
    t,
    messageId,
    visible,
    categoryName,
    triage?.category_id,
    correct,
    byCategory,
    onViewChange,
    navigate,
  ]);
  useCommands(commands);

  return (
    <CategoryPicker
      open={pickerOpen}
      onOpenChange={setPickerOpen}
      categories={visible}
      currentId={triage?.category_id}
      onSelect={correct}
    />
  );
}
