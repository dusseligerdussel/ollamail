import { Monitor, Moon, Sun } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useTheme } from "@/components/theme-provider";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { type SupportedLanguage, supportedLanguages } from "@/i18n";
import { isTheme, type Theme, themes } from "@/lib/theme";
import { cn } from "@/lib/utils";

export const themeIcons = { light: Sun, dark: Moon, system: Monitor } as const satisfies Record<
  Theme,
  unknown
>;

// Segmented control: a muted track with the selected option raised.
const groupClass = "h-8 w-fit rounded-md bg-muted p-0.5";
const itemClass =
  "h-7 flex-1 gap-1.5 rounded-[calc(var(--radius)-3px)] px-3 text-ui font-normal text-muted-foreground hover:bg-transparent hover:text-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow-xs dark:data-[state=on]:bg-input/30";

/** Segmented control for light, dark and system theme. */
export function ThemeToggleGroup({ className }: { className?: string }) {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();

  return (
    <ToggleGroup
      type="single"
      spacing={0.5}
      value={theme}
      onValueChange={(value) => isTheme(value) && setTheme(value)}
      aria-label={t("theme.label")}
      className={cn(groupClass, className)}
    >
      {themes.map((option) => {
        const Icon = themeIcons[option];
        return (
          <ToggleGroupItem key={option} value={option} className={itemClass}>
            <Icon aria-hidden="true" />
            {t(`theme.${option}`)}
          </ToggleGroupItem>
        );
      })}
    </ToggleGroup>
  );
}

/**
 * Segmented control for the interface language. `onChange` replaces the default (switch the
 * language only), e.g. to also store it in the profile.
 */
export function LanguageToggleGroup({
  className,
  onChange,
}: {
  className?: string;
  onChange?: (language: SupportedLanguage) => void;
}) {
  const { t, i18n } = useTranslation();

  return (
    <ToggleGroup
      type="single"
      spacing={0.5}
      value={i18n.resolvedLanguage}
      onValueChange={(value) => {
        if ((supportedLanguages as readonly string[]).includes(value)) {
          const language = value as SupportedLanguage;
          if (onChange) onChange(language);
          else void i18n.changeLanguage(language);
        }
      }}
      aria-label={t("language.label")}
      className={cn(groupClass, className)}
    >
      {supportedLanguages.map((language) => (
        <ToggleGroupItem key={language} value={language} lang={language} className={itemClass}>
          {t(`language.${language}`)}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
