import { Monitor, Moon, Sun } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useTheme } from "@/components/theme-provider";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { type SupportedLanguage, supportedLanguages } from "@/i18n";
import { isTheme, type Theme, themes } from "@/lib/theme";

export const themeIcons = { light: Sun, dark: Moon, system: Monitor } as const satisfies Record<
  Theme,
  unknown
>;

/** Segmented control for light, dark and system theme. */
export function ThemeToggleGroup({ className }: { className?: string }) {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();

  return (
    <ToggleGroup
      type="single"
      variant="segmented"
      spacing={0.5}
      value={theme}
      onValueChange={(value) => isTheme(value) && setTheme(value)}
      aria-label={t("theme.label")}
      className={className}
    >
      {themes.map((option) => {
        const Icon = themeIcons[option];
        return (
          <ToggleGroupItem key={option} value={option} className="gap-1.5">
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
      variant="segmented"
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
      className={className}
    >
      {supportedLanguages.map((language) => (
        <ToggleGroupItem key={language} value={language} lang={language}>
          {t(`language.${language}`)}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
