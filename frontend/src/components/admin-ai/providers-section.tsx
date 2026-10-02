import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type AIProvider,
  aiProviderModelsQueryOptions,
  testProvider,
  useDeleteProvider,
} from "@/api/ai";
import { describeApiError, isApiError } from "@/api/errors";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { ProviderSheet } from "./provider-sheet";
import { AdminSection } from "./section";
import { TestResult } from "./test-result";

function ProviderRow({
  provider,
  onEdit,
  onDelete,
}: {
  provider: AIProvider;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const test = useMutation({
    mutationFn: () => testProvider(provider.name),
    onSuccess: (result) =>
      queryClient.setQueryData(aiProviderModelsQueryOptions(provider.name).queryKey, result),
  });
  const fromEnvironment = provider.source === "environment";
  const usage =
    provider.used_by.length > 0
      ? t("pages.ai.providers.usedBy", {
          tasks: provider.used_by.map((task) => t(`pages.ai.tasks.${task}`)).join(", "),
        })
      : t("pages.ai.providers.unused");

  return (
    <li className="flex flex-col gap-2 px-4 py-3.5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-ui font-medium">{provider.display_name}</span>
            <Badge variant="outline">
              {t(provider.is_cloud ? "pages.ai.providers.cloud" : "pages.ai.providers.local")}
            </Badge>
            {fromEnvironment && (
              <Badge variant="secondary" title={t("pages.ai.providers.environmentHint")}>
                {t("pages.ai.providers.environment")}
              </Badge>
            )}
          </div>
          <p className="truncate text-ui text-muted-foreground">
            {t(`pages.ai.providers.kinds.${provider.kind}`)} · {provider.base_url}
          </p>
          <p className="text-xs text-muted-foreground">
            {usage}
            {provider.api_key_set && ` · ${t("pages.ai.providers.apiKeySet")}`}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            disabled={test.isPending}
            onClick={() => test.mutate()}
          >
            {t(test.isPending ? "pages.ai.providers.testing" : "pages.ai.providers.test")}
          </Button>
          {!fromEnvironment && (
            <>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`${t("pages.ai.providers.edit")}: ${provider.display_name}`}
                onClick={onEdit}
              >
                <Pencil />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`${t("pages.ai.providers.delete")}: ${provider.display_name}`}
                onClick={onDelete}
              >
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </div>
      {test.data && <TestResult result={test.data} />}
    </li>
  );
}

function DeleteDialog({
  provider,
  onOpenChange,
}: {
  provider: AIProvider | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const remove = useDeleteProvider();
  const inUse = isApiError(remove.error) && remove.error.status === 409;
  return (
    <Dialog
      open={provider !== null}
      onOpenChange={(open) => {
        if (!open) remove.reset();
        onOpenChange(open);
      }}
    >
      <DialogContent closeLabel={t("common.close")}>
        <DialogHeader>
          <DialogTitle>
            {t("pages.ai.providers.deleteTitle", { name: provider?.display_name ?? "" })}
          </DialogTitle>
          <DialogDescription>{t("pages.ai.providers.deleteDescription")}</DialogDescription>
        </DialogHeader>
        {remove.error && (
          <p role="alert" className="text-ui text-destructive">
            {inUse ? t("pages.ai.providers.inUse") : describeApiError(remove.error, t).title}
          </p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">{t("pages.ai.form.cancel")}</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={remove.isPending || !provider}
            onClick={() =>
              provider &&
              remove.mutate(provider.name, {
                onSuccess: () => {
                  toast.success(t("pages.ai.providers.deleted"));
                  onOpenChange(false);
                },
              })
            }
          >
            {t("pages.ai.providers.delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ProvidersSection({ providers }: { providers: AIProvider[] }) {
  const { t } = useTranslation();
  const id = useId();
  const [sheet, setSheet] = useState<{ open: boolean; provider: AIProvider | null }>({
    open: false,
    provider: null,
  });
  const [deleting, setDeleting] = useState<AIProvider | null>(null);

  return (
    <AdminSection
      id={`${id}-title`}
      title={t("pages.ai.providers.section")}
      actions={
        <Button
          size="sm"
          variant="outline"
          onClick={() => setSheet({ open: true, provider: null })}
        >
          <Plus />
          {t("pages.ai.providers.add")}
        </Button>
      }
    >
      <ul aria-labelledby={`${id}-title`} className="divide-y">
        {providers.map((provider) => (
          <ProviderRow
            key={provider.name}
            provider={provider}
            onEdit={() => setSheet({ open: true, provider })}
            onDelete={() => setDeleting(provider)}
          />
        ))}
      </ul>
      <ProviderSheet
        open={sheet.open}
        provider={sheet.provider}
        onOpenChange={(open) => setSheet((previous) => ({ ...previous, open }))}
      />
      <DeleteDialog provider={deleting} onOpenChange={(open) => !open && setDeleting(null)} />
    </AdminSection>
  );
}
