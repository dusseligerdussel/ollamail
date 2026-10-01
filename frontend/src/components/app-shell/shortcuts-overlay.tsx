import { createContext, type ReactNode, useContext, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { KeyHint } from "@/components/key-hint";
import { useRegisteredShortcuts } from "@/components/shortcuts/shortcut-provider";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { shortcutGroups } from "@/lib/shortcuts";

interface OverlayState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

const OverlayContext = createContext<OverlayState | null>(null);

export function ShortcutsOverlayProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const value = useMemo(() => ({ open, setOpen }), [open]);
  return <OverlayContext value={value}>{children}</OverlayContext>;
}

export function useShortcutsOverlay(): OverlayState {
  const context = useContext(OverlayContext);
  if (!context)
    throw new Error("useShortcutsOverlay must be used within <ShortcutsOverlayProvider>");
  return context;
}

/** Side sheet listing every registered shortcut, grouped (opened with `?`). */
export function ShortcutsOverlay() {
  const { t } = useTranslation();
  const { open, setOpen } = useShortcutsOverlay();
  const shortcuts = useRegisteredShortcuts();

  const groups = shortcutGroups
    .map((group) => ({
      group,
      items: shortcuts.filter((shortcut) => shortcut.group === group && !shortcut.hidden),
    }))
    .filter(({ items }) => items.length > 0);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent side="right" closeLabel={t("common.close")} className="gap-0 sm:max-w-sm">
        <SheetHeader className="border-b px-5 py-4">
          <SheetTitle className="text-sm">{t("shortcuts.title")}</SheetTitle>
          <SheetDescription className="text-ui">{t("shortcuts.description")}</SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {groups.map(({ group, items }) => (
            <section key={group} className="mb-6 last:mb-0" aria-labelledby={`shortcuts-${group}`}>
              <h3
                id={`shortcuts-${group}`}
                className="mb-1 text-xs font-medium text-muted-foreground"
              >
                {t(`shortcuts.groups.${group}`)}
              </h3>
              <dl>
                {items.map((shortcut) => (
                  <div
                    key={shortcut.id}
                    className="flex h-9 items-center justify-between gap-4 border-b border-border/60 last:border-b-0"
                  >
                    <dt className="text-ui">{shortcut.description}</dt>
                    <dd>
                      <KeyHint keys={shortcut.keys} />
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
