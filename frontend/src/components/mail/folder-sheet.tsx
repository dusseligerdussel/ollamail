import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type Folder,
  foldersQueryOptions,
  type Mailbox,
  mailKeys,
  selectFolders,
} from "@/api/mail";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { useMailErrorText } from "@/components/mail/sync-status";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

interface FolderSheetProps {
  mailbox: Mailbox | undefined;
  onOpenChange: (open: boolean) => void;
}

/** Choose which folders of a mailbox are synced. */
export function FolderSheet({ mailbox, onOpenChange }: FolderSheetProps) {
  const { t } = useTranslation();
  return (
    <Sheet open={!!mailbox} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col gap-0 sm:max-w-md">
        <SheetHeader className="border-b">
          <SheetTitle>{t("mailboxes.folders.title")}</SheetTitle>
          <SheetDescription>{mailbox?.display_name}</SheetDescription>
        </SheetHeader>
        {mailbox && (
          <FolderSelection
            key={mailbox.id}
            mailboxId={mailbox.id}
            onDone={() => onOpenChange(false)}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

function FolderSelection({ mailboxId, onDone }: { mailboxId: string; onDone: () => void }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const errorText = useMailErrorText();
  const folders = useQuery(foldersQueryOptions(mailboxId));
  const [changes, setChanges] = useState<Record<string, boolean>>({});
  const save = useMutation({
    mutationFn: () =>
      selectFolders(
        mailboxId,
        Object.entries(changes).map(([id, sync_enabled]) => ({ id, sync_enabled })),
      ),
    onSuccess: (data) => {
      queryClient.setQueryData(mailKeys.folders(mailboxId), data);
      void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
      void queryClient.invalidateQueries({ queryKey: ["message"] });
      toast.success(t("mailboxes.folders.saved"));
      onDone();
    },
  });

  const checked = (folder: Folder) => changes[folder.id] ?? folder.sync_enabled;
  const numbers = new Intl.NumberFormat(i18n.language);

  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {folders.isPending && <ListSkeleton rows={6} />}
        {folders.isError && <InlineError error={folders.error} className="m-4" />}
        {folders.data?.length === 0 && (
          <p className="px-4 py-6 text-ui text-muted-foreground">{t("mailboxes.folders.empty")}</p>
        )}
        {folders.data && folders.data.length > 0 && (
          <ul className="divide-y">
            {folders.data.map((folder) => {
              const id = `folder-${folder.id}`;
              return (
                <li key={folder.id} className="flex items-start gap-3 px-4 py-2.5">
                  <Checkbox
                    id={id}
                    className="mt-0.5"
                    checked={checked(folder) && !folder.excluded_by_role}
                    disabled={folder.excluded_by_role}
                    onCheckedChange={(value) =>
                      setChanges((current) => ({ ...current, [folder.id]: value === true }))
                    }
                  />
                  <label htmlFor={id} className="min-w-0 flex-1 text-ui">
                    <span className="block truncate font-medium">{folder.name}</span>
                    <span className="block truncate text-muted-foreground">
                      {folder.excluded_by_role
                        ? t("mailboxes.folders.excluded")
                        : folder.last_error
                          ? errorText(folder.last_error)
                          : folder.import_pending && folder.synced
                            ? t("mailboxes.folders.importing")
                            : t("mailboxes.folders.messages", {
                                count: folder.message_count,
                                formatted: numbers.format(folder.message_count),
                              })}
                    </span>
                  </label>
                  {folder.role && (
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {t(`mailboxes.folders.roles.${folder.role}`)}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <SheetFooter className="flex-row justify-end border-t">
        <Button variant="outline" size="sm" onClick={onDone}>
          {t("mailboxes.cancel")}
        </Button>
        <Button
          size="sm"
          disabled={Object.keys(changes).length === 0 || save.isPending}
          onClick={() => save.mutate()}
        >
          {t("mailboxes.folders.save")}
        </Button>
      </SheetFooter>
    </>
  );
}
