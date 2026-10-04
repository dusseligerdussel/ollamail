import { useQuery } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type AIProvider,
  type AISettings,
  type AISettingsUpdate,
  aiProviderModelsQueryOptions,
  type LLMTask,
  useUpdateAISettings,
} from "@/api/ai";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { AdminSection } from "./section";

type Draft = Record<LLMTask, { provider: string; model: string }>;
type TaskSetting = AISettings["tasks"][number];

function draftFrom(settings: AISettings): Draft {
  return Object.fromEntries(
    settings.tasks.map((task) => [
      task.task,
      { provider: task.provider ?? "", model: task.model ?? "" },
    ]),
  ) as Draft;
}

/** Model names offered while typing; loaded from the provider on first focus. */
function ModelOptions({ id, provider }: { id: string; provider: string }) {
  const models = useQuery({
    ...aiProviderModelsQueryOptions(provider),
    enabled: Boolean(provider),
  });
  return (
    <datalist id={id}>
      {(models.data?.models ?? []).map((model) => (
        <option key={model} value={model} />
      ))}
    </datalist>
  );
}

function TaskRow({
  setting,
  providers,
  value,
  invalid,
  onChange,
}: {
  setting: TaskSetting;
  providers: AIProvider[];
  value: Draft[LLMTask];
  invalid: boolean;
  onChange: (value: Draft[LLMTask]) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [focused, setFocused] = useState(false);
  const provider = value.provider || setting.default_provider;
  const errorId = `${id}-error`;

  return (
    <div className="grid gap-3 px-4 py-3.5 sm:grid-cols-[minmax(0,1fr)_14rem_11rem] sm:items-start sm:gap-4">
      <div className="min-w-0">
        <div id={`${id}-label`} className="text-ui font-medium">
          {t(`pages.ai.tasks.${setting.task}`)}
        </div>
        <p className="text-ui text-muted-foreground">
          {t(`pages.ai.taskDescriptions.${setting.task}`)}
        </p>
        {setting.blocked && (
          <p className="mt-1 flex items-start gap-1.5 text-xs text-destructive">
            <TriangleAlert aria-hidden className="mt-px size-3.5 shrink-0" />
            {t("pages.ai.assignments.blocked")}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-provider`} className="text-xs text-muted-foreground sm:sr-only">
          {t("pages.ai.assignments.provider")}
        </label>
        <NativeSelect
          id={`${id}-provider`}
          size="sm"
          className="w-full"
          aria-describedby={`${id}-label`}
          value={value.provider}
          onChange={(event) => onChange({ ...value, provider: event.target.value })}
        >
          <NativeSelectOption value="">
            {t("pages.ai.assignments.defaultProvider", {
              name:
                providers.find((option) => option.name === setting.default_provider)
                  ?.display_name ?? setting.default_provider,
            })}
          </NativeSelectOption>
          {providers.map((option) => (
            <NativeSelectOption key={option.name} value={option.name}>
              {option.display_name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-model`} className="text-xs text-muted-foreground sm:sr-only">
          {t("pages.ai.assignments.model")}
        </label>
        <Input
          id={`${id}-model`}
          className="h-8"
          list={`${id}-models`}
          spellCheck={false}
          autoCapitalize="none"
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? errorId : `${id}-label`}
          placeholder={
            value.provider
              ? undefined
              : t("pages.ai.assignments.modelPlaceholder", { model: setting.default_model })
          }
          value={value.model}
          onFocus={() => setFocused(true)}
          onChange={(event) => onChange({ ...value, model: event.target.value })}
        />
        {focused && <ModelOptions id={`${id}-models`} provider={provider} />}
        {invalid && (
          <p id={errorId} className="text-xs text-destructive">
            {t("pages.ai.assignments.modelRequired")}
          </p>
        )}
      </div>
    </div>
  );
}

/** Provider and model per task; empty fields fall back to the environment's default. */
export function AssignmentsSection({
  settings,
  providers,
}: {
  settings: AISettings;
  providers: AIProvider[];
}) {
  const { t } = useTranslation();
  const id = useId();
  const update = useUpdateAISettings();
  const [draft, setDraft] = useState(() => draftFrom(settings));
  useEffect(() => setDraft(draftFrom(settings)), [settings]);

  const saved = draftFrom(settings);
  const changed = settings.tasks
    .map((task) => task.task)
    .filter(
      (task) =>
        draft[task].provider !== saved[task].provider ||
        draft[task].model.trim() !== saved[task].model,
    );
  const invalid = (task: LLMTask) => Boolean(draft[task].provider) && !draft[task].model.trim();
  const hasErrors = changed.some(invalid);

  const save = () => {
    const tasks: NonNullable<AISettingsUpdate["tasks"]> = {};
    for (const task of changed) {
      const { provider, model } = draft[task];
      tasks[task] = { provider: provider || null, model: model.trim() || null };
    }
    update.mutate({ tasks }, { onSuccess: () => toast.success(t("pages.ai.assignments.saved")) });
  };

  return (
    <AdminSection id={`${id}-title`} title={t("pages.ai.assignments.section")}>
      <div
        aria-hidden
        className="hidden grid-cols-[minmax(0,1fr)_14rem_11rem] gap-4 px-4 py-2 text-xs text-muted-foreground sm:grid"
      >
        <span />
        <span>{t("pages.ai.assignments.provider")}</span>
        <span>{t("pages.ai.assignments.model")}</span>
      </div>
      {settings.tasks.map((setting) => (
        <TaskRow
          key={setting.task}
          setting={setting}
          providers={providers}
          value={draft[setting.task]}
          invalid={invalid(setting.task)}
          onChange={(value) => setDraft((previous) => ({ ...previous, [setting.task]: value }))}
        />
      ))}
      <div className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs text-muted-foreground">{t("pages.ai.assignments.hint")}</p>
        <div className="flex shrink-0 gap-2">
          {changed.length > 0 && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(saved)}>
              {t("pages.ai.assignments.reset")}
            </Button>
          )}
          <Button
            size="sm"
            disabled={changed.length === 0 || hasErrors || update.isPending}
            onClick={save}
          >
            {t("pages.ai.assignments.save")}
          </Button>
        </div>
      </div>
    </AdminSection>
  );
}
