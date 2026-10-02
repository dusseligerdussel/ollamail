import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { type AISettings, type LLMProfile, useUpdateAISettings } from "@/api/ai";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { AdminSection } from "./section";

/** Hardware profile and parallel LLM requests per worker. */
export function PerformanceSection({ settings }: { settings: AISettings }) {
  const { t } = useTranslation();
  const id = useId();
  const update = useUpdateAISettings();
  const [profile, setProfile] = useState<LLMProfile>(settings.profile);
  const [concurrency, setConcurrency] = useState(String(settings.concurrency));
  useEffect(() => {
    setProfile(settings.profile);
    setConcurrency(String(settings.concurrency));
  }, [settings]);

  const parsed = Number(concurrency);
  const validConcurrency =
    Number.isInteger(parsed) && parsed >= 1 && parsed <= settings.concurrency_max;
  const dirty = profile !== settings.profile || parsed !== settings.concurrency;

  return (
    <AdminSection id={`${id}-title`} title={t("pages.ai.performance.section")}>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <label htmlFor={`${id}-profile`} className="text-ui font-medium">
            {t("pages.ai.performance.profile")}
          </label>
          <p className="text-ui text-muted-foreground">
            {t("pages.ai.performance.profileDescription")}
          </p>
        </div>
        <NativeSelect
          id={`${id}-profile`}
          size="sm"
          className="w-full shrink-0 sm:w-72"
          value={profile}
          onChange={(event) => setProfile(event.target.value as LLMProfile)}
        >
          {settings.profiles.map((option) => (
            <NativeSelectOption key={option.name} value={option.name}>
              {t("pages.ai.performance.profileOption", {
                label: t(`pages.ai.performance.profiles.${option.name}`),
                model: option.chat_model,
                tokens: option.context_tokens,
              })}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <label htmlFor={`${id}-concurrency`} className="text-ui font-medium">
            {t("pages.ai.performance.concurrency")}
          </label>
          <p className="text-ui text-muted-foreground">
            {t("pages.ai.performance.concurrencyDescription", { max: settings.concurrency_max })}
          </p>
        </div>
        <Input
          id={`${id}-concurrency`}
          type="number"
          inputMode="numeric"
          min={1}
          max={settings.concurrency_max}
          className="h-8 w-full shrink-0 sm:w-24"
          aria-invalid={!validConcurrency || undefined}
          value={concurrency}
          onChange={(event) => setConcurrency(event.target.value)}
        />
      </div>
      <div className="flex justify-end px-4 py-3">
        <Button
          size="sm"
          disabled={!dirty || !validConcurrency || update.isPending}
          onClick={() =>
            update.mutate(
              { profile, concurrency: parsed },
              { onSuccess: () => toast.success(t("pages.ai.performance.saved")) },
            )
          }
        >
          {t("pages.ai.performance.save")}
        </Button>
      </div>
    </AdminSection>
  );
}
