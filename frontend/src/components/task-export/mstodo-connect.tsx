import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { pageNavigation } from "@/api/auth";
import { problemErrorCode } from "@/api/mail";
import {
  type ExportMode,
  type ExportTarget,
  listMsTodoLists,
  msTodoListsKey,
  startMsTodoConnect,
  useSaveMsTodo,
} from "@/api/todo-export";
import { FormError } from "@/components/form-field";
import { ModeChoice, useExportErrorText } from "@/components/task-export/connect-form";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";

/** Where Microsoft sends the browser back to (`?mstodo=connected` or `?mstodo=error`). */
export const MSTODO_RETURN_PATH = "/settings/task-export";

interface MsTodoConnectProps {
  current?: ExportTarget;
  /** Just back from the Microsoft sign-in; the tokens wait on the server for the list. */
  signedIn?: boolean;
  onDone: () => void;
  onCancel?: () => void;
}

/**
 * Microsoft To Do: sign in with Microsoft (no URL or password), then list and mode. Nothing is
 * stored before "Turn on export".
 */
export function MsTodoConnect({ current, signedIn, onDone, onCancel }: MsTodoConnectProps) {
  const { t } = useTranslation();
  const errorText = useExportErrorText();
  const connected = !!signedIn || current?.sink === "mstodo";
  const [listId, setListId] = useState(current?.sink === "mstodo" ? current.list_id : "");
  const [mode, setMode] = useState<ExportMode>(current?.mode ?? "auto");
  const save = useSaveMsTodo();

  const start = useMutation({
    mutationFn: () => startMsTodoConnect(MSTODO_RETURN_PATH),
    meta: { errorToast: false },
    onSuccess: ({ authorization_url }) => pageNavigation.assign(authorization_url),
  });
  const lists = useQuery({
    queryKey: msTodoListsKey,
    queryFn: listMsTodoLists,
    enabled: connected,
    retry: false,
    gcTime: 0,
  });

  const found = lists.data?.lists ?? [];
  const selected = found.some((list) => list.id === listId) ? listId : (found[0]?.id ?? "");
  const startError = start.isError ? errorText(problemErrorCode(start.error)) : undefined;

  const signIn = (label: string, variant: "default" | "outline" = "default") => (
    <Button
      type="button"
      size="sm"
      variant={variant}
      disabled={start.isPending}
      onClick={() => start.mutate()}
    >
      {start.isPending ? t("taskExport.mstodo.redirecting") : label}
    </Button>
  );

  if (!connected || lists.isError) {
    return (
      <div className="flex flex-col gap-4">
        <p className="text-ui text-muted-foreground">{t("taskExport.mstodo.signInHint")}</p>
        {lists.isError && <FormError>{errorText(problemErrorCode(lists.error))}</FormError>}
        {startError && <FormError>{startError}</FormError>}
        <div className="flex gap-2">
          {signIn(t("taskExport.mstodo.signIn"))}
          {onCancel && (
            <Button type="button" size="sm" variant="outline" onClick={onCancel}>
              {t("taskExport.cancel")}
            </Button>
          )}
        </div>
      </div>
    );
  }

  if (lists.isPending) {
    return (
      <div
        role="status"
        aria-label={t("taskExport.mstodo.loadingLists")}
        className="flex flex-col gap-3"
      >
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  const onSave = (event: FormEvent) => {
    event.preventDefault();
    save.mutate({ list_id: selected, mode }, { onSuccess: onDone });
  };

  return (
    <form onSubmit={onSave} className="flex flex-col gap-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <p className="min-w-0 text-ui break-words">
          {t("taskExport.mstodo.signedInAs", { account: lists.data.account })}
        </p>
        {signIn(t("taskExport.mstodo.otherAccount"), "outline")}
      </div>
      {startError && <FormError>{startError}</FormError>}
      {found.length === 0 ? (
        <FormError>{t("taskExport.noLists")}</FormError>
      ) : (
        <div className="flex flex-col gap-2">
          <Label htmlFor="task-export-mstodo-list" className="text-ui">
            {t("taskExport.list")}
          </Label>
          <NativeSelect
            id="task-export-mstodo-list"
            className="w-full"
            value={selected}
            onChange={(event) => setListId(event.target.value)}
          >
            {found.map((list) => (
              <NativeSelectOption key={list.id} value={list.id}>
                {list.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      )}
      <div className="flex flex-col gap-2">
        <div className="text-ui">{t("taskExport.mode.label")}</div>
        <ModeChoice value={mode} onChange={setMode} />
      </div>
      {save.isError && <FormError>{errorText(problemErrorCode(save.error))}</FormError>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={!selected || save.isPending}>
          {current ? t("taskExport.save") : t("taskExport.enable")}
        </Button>
        {onCancel && (
          <Button type="button" size="sm" variant="outline" onClick={onCancel}>
            {t("taskExport.cancel")}
          </Button>
        )}
      </div>
    </form>
  );
}
