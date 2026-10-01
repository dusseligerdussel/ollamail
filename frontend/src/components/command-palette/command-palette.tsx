import { Check } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

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
import { groupCommands } from "@/lib/commands";

import { useCommandPalette, useRegisteredCommands } from "./command-provider";

/**
 * Tracks whether a scroll container has content hidden above or below its visible area, so the
 * edges can hint at it (`data-overflow` = `top`, `bottom` or `both`).
 */
function useScrollOverflow<T extends HTMLElement>() {
  const [element, setElement] = useState<T | null>(null);
  const [overflow, setOverflow] = useState<"top" | "bottom" | "both" | undefined>();

  const update = useCallback(() => {
    if (!element) return;
    const top = element.scrollTop > 1;
    const bottom = element.scrollTop + element.clientHeight < element.scrollHeight - 1;
    setOverflow(top && bottom ? "both" : top ? "top" : bottom ? "bottom" : undefined);
  }, [element]);

  useEffect(() => {
    if (!element) return;
    update();
    element.addEventListener("scroll", update, { passive: true });
    // Filtering changes the content height without scrolling.
    const observer = new ResizeObserver(update);
    observer.observe(element);
    for (const child of element.children) observer.observe(child);
    return () => {
      element.removeEventListener("scroll", update);
      observer.disconnect();
    };
  }, [element, update]);

  return { ref: setElement, overflow };
}

/** ⌘K / Ctrl+K palette listing all registered commands. */
export function CommandPalette() {
  const { t } = useTranslation();
  const { open, setOpen } = useCommandPalette();
  const commands = useRegisteredCommands();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const list = useScrollOverflow<HTMLDivElement>();

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent
        showCloseButton={false}
        className="top-[18%] translate-y-0 gap-0 overflow-hidden p-0 sm:max-w-xl"
      >
        <DialogTitle className="sr-only">{t("commandPalette.title")}</DialogTitle>
        <DialogDescription className="sr-only">{t("commandPalette.description")}</DialogDescription>
        <Command
          loop
          // Ctrl+J/K would otherwise swallow Ctrl+K, which closes the palette again.
          vimBindings={false}
          className="**:data-[slot=command-input-wrapper]:h-12 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground"
        >
          <CommandInput
            placeholder={t("commandPalette.placeholder")}
            aria-label={t("commandPalette.placeholder")}
            className="h-12"
          />
          <CommandList
            ref={list.ref}
            data-overflow={list.overflow}
            className="scroll-fade max-h-[min(380px,60vh)] p-1"
          >
            <CommandEmpty className="py-10 text-center text-ui text-muted-foreground">
              {t("commandPalette.empty")}
            </CommandEmpty>
            {groupCommands(commands).map(({ group, commands: items }) => (
              <CommandGroup key={group} heading={t(`commands.groups.${group}`)}>
                {items.map((command) => {
                  const Icon = command.icon;
                  return (
                    <CommandItem
                      key={command.id}
                      value={command.id}
                      keywords={[command.label, ...(command.keywords ?? [])]}
                      onSelect={() => {
                        setOpen(false);
                        command.run();
                      }}
                      className="h-9 gap-2.5 px-2 text-ui"
                    >
                      {Icon && <Icon className="size-4 text-muted-foreground" aria-hidden="true" />}
                      <span className="truncate">{command.label}</span>
                      <span className="ml-auto flex items-center gap-2">
                        {command.active && (
                          <Check className="size-4 text-brand" aria-hidden="true" />
                        )}
                        {hasKeyboard && command.shortcut && <KeyHint keys={command.shortcut} />}
                      </span>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
