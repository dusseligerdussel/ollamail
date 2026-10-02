import { Check } from "lucide-react";
import { type KeyboardEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Category } from "@/api/triage";
import { KeyHint } from "@/components/key-hint";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

import { useCategoryName } from "./use-triage";

// Categories reachable with a digit key (1–9).
const DIGITS = 9;

interface CategoryPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  categories: readonly Category[];
  currentId?: string | null;
  onSelect: (category: Category) => void;
}

/**
 * Choose a category with the keyboard: type to filter, `Enter` to pick, or a digit for one of
 * the first nine categories (so `c` then `2` corrects a message in two key presses).
 */
export function CategoryPicker({
  open,
  onOpenChange,
  categories,
  currentId,
  onSelect,
}: CategoryPickerProps) {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const [query, setQuery] = useState("");

  const choose = (category: Category) => {
    onOpenChange(false);
    onSelect(category);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (query || event.metaKey || event.ctrlKey || event.altKey) return;
    const digit = Number(event.key);
    if (!Number.isInteger(digit) || digit < 1 || digit > DIGITS) return;
    const category = categories[digit - 1];
    if (!category) return;
    event.preventDefault();
    choose(category);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setQuery("");
        onOpenChange(next);
      }}
    >
      <DialogContent
        showCloseButton={false}
        className="top-[18%] translate-y-0 gap-0 overflow-hidden p-0 sm:max-w-sm"
      >
        <DialogTitle className="sr-only">{t("triage.picker.title")}</DialogTitle>
        <DialogDescription className="sr-only">{t("triage.picker.description")}</DialogDescription>
        <Command
          loop
          vimBindings={false}
          onKeyDown={onKeyDown}
          defaultValue={currentId ?? undefined}
          className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground"
        >
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder={t("triage.picker.placeholder")}
            aria-label={t("triage.picker.placeholder")}
            className="h-11"
          />
          <CommandList className="max-h-[min(360px,60vh)] p-1">
            <CommandEmpty className="py-8 text-center text-ui text-muted-foreground">
              {t("triage.picker.empty")}
            </CommandEmpty>
            <CommandGroup heading={t("triage.picker.title")}>
              {categories.map((category, index) => (
                <CommandItem
                  key={category.id}
                  value={category.id}
                  keywords={[categoryName(category)]}
                  onSelect={() => choose(category)}
                  className="h-9 gap-2.5 px-2 text-ui"
                >
                  <span className="truncate">{categoryName(category)}</span>
                  <span className="ml-auto flex items-center gap-2">
                    {category.id === currentId && (
                      <Check className="size-4 text-brand" aria-hidden="true" />
                    )}
                    {hasKeyboard && index < DIGITS && <KeyHint keys={String(index + 1)} />}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
