import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useShortcut } from "@/components/shortcuts/shortcut-provider";

interface ListNavigationOptions {
  /** Number of items in the list. */
  count: number;
  /** Called with the active index when the user presses `o`. */
  onOpen?: (index: number) => void;
}

/** Registers `j`/`k` (next/previous) and `o` (open) for a list. */
export function useListNavigation({ count, onOpen }: ListNavigationOptions) {
  const { t } = useTranslation();
  const [activeIndex, setActiveIndex] = useState(-1);

  useEffect(() => {
    setActiveIndex((index) => Math.min(index, count - 1));
  }, [count]);

  useShortcut(
    { id: "list.next", keys: "j", group: "list", description: t("shortcuts.list.next") },
    () => setActiveIndex((index) => Math.min(index + 1, count - 1)),
  );
  useShortcut(
    { id: "list.previous", keys: "k", group: "list", description: t("shortcuts.list.previous") },
    () => setActiveIndex((index) => (count === 0 ? -1 : Math.max(index - 1, 0))),
  );
  useShortcut(
    { id: "list.open", keys: "o", group: "list", description: t("shortcuts.list.open") },
    () => {
      if (activeIndex >= 0) onOpen?.(activeIndex);
    },
  );

  return { activeIndex, setActiveIndex };
}
