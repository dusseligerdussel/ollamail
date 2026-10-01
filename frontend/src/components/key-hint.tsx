import { Fragment } from "react";
import { useTranslation } from "react-i18next";

import { Kbd, KbdGroup } from "@/components/ui/kbd";
import { formatKeys } from "@/lib/shortcuts";
import { cn } from "@/lib/utils";

/** Renders a key binding such as `mod+k` or `g i` as keyboard keys. */
export function KeyHint({ keys, className }: { keys: string; className?: string }) {
  const { t } = useTranslation();
  const steps = formatKeys(keys);

  return (
    <KbdGroup className={cn("font-sans text-muted-foreground", className)}>
      {steps.map((step, stepIndex) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: steps have no identity besides position
        <Fragment key={stepIndex}>
          {stepIndex > 0 && (
            <span className="px-0.5 text-xs font-normal">{t("shortcuts.then")}</span>
          )}
          {step.map((label) => (
            <Kbd key={label}>{label}</Kbd>
          ))}
        </Fragment>
      ))}
    </KbdGroup>
  );
}
