import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type AIProvider,
  type AIProviderTest,
  type LLMProviderKind,
  testProviderSettings,
  useCreateProvider,
  useUpdateProvider,
} from "@/api/ai";
import { describeApiError, isApiError } from "@/api/errors";
import { isReauthCancelled } from "@/api/reauth";
import { useReauth } from "@/components/auth/reauth";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";

import { TestResult } from "./test-result";

const NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;

interface FormState {
  name: string;
  displayName: string;
  kind: LLMProviderKind;
  baseUrl: string;
  apiKey: string;
  removeApiKey: boolean;
  isCloud: boolean;
  structuredOutput: "native" | "prompt";
  timeout: string;
}

function initialState(provider: AIProvider | null): FormState {
  return {
    name: provider?.name ?? "",
    displayName: provider?.display_name ?? "",
    kind: provider?.kind ?? "ollama",
    baseUrl: provider?.base_url ?? "",
    apiKey: "",
    removeApiKey: false,
    isCloud: provider?.is_cloud ?? false,
    structuredOutput: provider?.structured_output ?? "native",
    timeout: provider?.timeout ? String(provider.timeout) : "",
  };
}

function validUrl(value: string) {
  try {
    const url = new URL(value.trim());
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username;
  } catch {
    return false;
  }
}

/**
 * Add (`provider` null) or edit a provider in a side sheet. The API key is write-only: when
 * editing, an empty field keeps the stored key.
 */
export function ProviderSheet({
  open,
  provider,
  onOpenChange,
}: {
  open: boolean;
  provider: AIProvider | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const editing = provider !== null;
  const [form, setForm] = useState(() => initialState(provider));
  const [submitted, setSubmitted] = useState(false);
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const withReauth = useReauth();
  const test = useMutation({
    // The stored key goes to another URL or type only after a recent confirmation (#219).
    mutationFn: (body: AIProviderTest) => withReauth(() => testProviderSettings(body)),
    meta: { errorToast: false },
  });
  const { reset: resetTest } = test;

  useEffect(() => {
    if (open) {
      setForm(initialState(provider));
      setSubmitted(false);
      resetTest();
    }
  }, [open, provider, resetTest]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((previous) => ({ ...previous, [key]: value }));

  const nameError =
    !editing && (submitted || form.name) && !NAME.test(form.name)
      ? t("pages.ai.form.nameInvalid")
      : undefined;
  const urlError =
    (submitted || form.baseUrl) && !validUrl(form.baseUrl)
      ? t("pages.ai.form.baseUrlInvalid")
      : undefined;
  const timeout = form.timeout ? Number(form.timeout) : null;
  const save = editing ? update : create;
  const saveError = save.error;
  const nameTaken = isApiError(saveError) && saveError.status === 409;

  // Keeping the stored key while the destination changes: the test asks for a confirmation.
  const keyToNewTarget =
    editing &&
    provider.api_key_set &&
    !form.apiKey &&
    !form.removeApiKey &&
    (form.kind !== provider.kind || form.baseUrl.trim().replace(/\/+$/, "") !== provider.base_url);
  const testError = test.error && !isReauthCancelled(test.error) ? test.error : null;

  const testBody = (): AIProviderTest => ({
    kind: form.kind,
    base_url: form.baseUrl.trim(),
    ...(form.apiKey && { api_key: form.apiKey }),
    ...(editing && !form.apiKey && !form.removeApiKey && { name: provider.name }),
    ...(timeout && { timeout }),
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if ((!editing && !NAME.test(form.name)) || !validUrl(form.baseUrl) || !form.displayName.trim())
      return;
    const common = {
      display_name: form.displayName.trim(),
      kind: form.kind,
      base_url: form.baseUrl.trim(),
      is_cloud: form.isCloud,
      structured_output: form.structuredOutput,
      timeout,
    };
    const done = (message: string) => {
      toast.success(message);
      onOpenChange(false);
    };
    if (editing) {
      update.mutate(
        {
          name: provider.name,
          body: {
            ...common,
            ...(form.apiKey ? { api_key: form.apiKey } : form.removeApiKey && { api_key: null }),
          },
        },
        { onSuccess: () => done(t("pages.ai.form.updated")) },
      );
    } else {
      create.mutate(
        { ...common, name: form.name, ...(form.apiKey && { api_key: form.apiKey }) },
        { onSuccess: () => done(t("pages.ai.form.created")) },
      );
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        closeLabel={t("common.close")}
        className="w-full gap-0 sm:max-w-md"
        aria-describedby={`${id}-description`}
      >
        <SheetHeader className="border-b">
          <SheetTitle>
            {t(editing ? "pages.ai.form.editTitle" : "pages.ai.form.addTitle")}
          </SheetTitle>
          <SheetDescription id={`${id}-description`}>
            {t("pages.ai.form.description")}
          </SheetDescription>
        </SheetHeader>
        <form
          noValidate
          onSubmit={onSubmit}
          className="flex min-h-0 flex-1 flex-col"
          aria-label={t(editing ? "pages.ai.form.editTitle" : "pages.ai.form.addTitle")}
        >
          <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
            <FormField
              label={t("pages.ai.form.displayName")}
              value={form.displayName}
              onChange={(event) => set("displayName", event.target.value)}
              maxLength={255}
              required
              autoFocus
              error={
                submitted && !form.displayName.trim()
                  ? t("pages.ai.form.displayNameRequired")
                  : undefined
              }
            />
            <FormField
              label={t("pages.ai.form.name")}
              value={form.name}
              onChange={(event) => set("name", event.target.value)}
              description={t("pages.ai.form.nameHint")}
              error={nameError ?? (nameTaken ? t("pages.ai.form.nameTaken") : undefined)}
              disabled={editing}
              maxLength={32}
              autoCapitalize="none"
              spellCheck={false}
              required
            />
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${id}-kind`} className="text-ui">
                {t("pages.ai.form.kind")}
              </Label>
              <NativeSelect
                id={`${id}-kind`}
                className="w-full"
                value={form.kind}
                onChange={(event) => set("kind", event.target.value as LLMProviderKind)}
              >
                {(["ollama", "openai_compatible"] as const).map((kind) => (
                  <NativeSelectOption key={kind} value={kind}>
                    {t(`pages.ai.providers.kinds.${kind}`)}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </div>
            <FormField
              label={t("pages.ai.form.baseUrl")}
              type="url"
              value={form.baseUrl}
              onChange={(event) => set("baseUrl", event.target.value)}
              description={t(
                form.kind === "ollama"
                  ? "pages.ai.form.baseUrlHintOllama"
                  : "pages.ai.form.baseUrlHintOpenai",
              )}
              error={urlError}
              spellCheck={false}
              required
            />
            <FormField
              label={t("pages.ai.form.apiKey")}
              type="password"
              autoComplete="off"
              value={form.apiKey}
              onChange={(event) => set("apiKey", event.target.value)}
              description={t(
                keyToNewTarget
                  ? "pages.ai.form.apiKeyNewTarget"
                  : editing && provider.api_key_set && !form.removeApiKey
                    ? "pages.ai.form.apiKeyKeep"
                    : "pages.ai.form.apiKeyHint",
              )}
            />
            {editing && provider.api_key_set && !form.apiKey && (
              <div className="-mt-2 flex items-center gap-2">
                <Switch
                  id={`${id}-remove-key`}
                  checked={form.removeApiKey}
                  onCheckedChange={(value) => set("removeApiKey", value)}
                />
                <Label htmlFor={`${id}-remove-key`} className="text-ui font-normal">
                  {t("pages.ai.form.removeApiKey")}
                </Label>
              </div>
            )}
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <Label htmlFor={`${id}-cloud`} className="text-ui">
                  {t("pages.ai.form.isCloud")}
                </Label>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t("pages.ai.form.isCloudHint")}
                </p>
              </div>
              <Switch
                id={`${id}-cloud`}
                checked={form.isCloud}
                onCheckedChange={(value) => set("isCloud", value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${id}-structured`} className="text-ui">
                {t("pages.ai.form.structuredOutput")}
              </Label>
              <NativeSelect
                id={`${id}-structured`}
                className="w-full"
                value={form.structuredOutput}
                onChange={(event) =>
                  set("structuredOutput", event.target.value as FormState["structuredOutput"])
                }
              >
                <NativeSelectOption value="native">
                  {t("pages.ai.form.structuredNative")}
                </NativeSelectOption>
                <NativeSelectOption value="prompt">
                  {t("pages.ai.form.structuredPrompt")}
                </NativeSelectOption>
              </NativeSelect>
            </div>
            <FormField
              label={t("pages.ai.form.timeout")}
              type="number"
              inputMode="numeric"
              min={1}
              max={3600}
              value={form.timeout}
              onChange={(event) => set("timeout", event.target.value)}
              description={t("pages.ai.form.timeoutHint")}
            />
            <div className="flex flex-col items-start gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={!validUrl(form.baseUrl) || test.isPending}
                onClick={() => test.mutate(testBody())}
              >
                {t(test.isPending ? "pages.ai.providers.testing" : "pages.ai.providers.test")}
              </Button>
              {test.data && <TestResult result={test.data} />}
              {testError && <FormError>{describeApiError(testError, t).title}</FormError>}
            </div>
            {saveError && !nameTaken && (
              <FormError>{describeApiError(saveError, t).title}</FormError>
            )}
          </div>
          <SheetFooter className="flex-row justify-end border-t">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t("pages.ai.form.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending}>
              {t(save.isPending ? "pages.ai.form.saving" : "pages.ai.form.save")}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}
