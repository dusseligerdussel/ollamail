import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { type AdminUser, isAdminLockout } from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { isLastAdmin, type UserDeletionResult, useDeleteUser } from "@/api/privacy";
import { Notice } from "@/components/admin/notice";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** What a deletion removes, in display order (`pages.users.delete.items.*`). */
const deletedItems = ["mailboxes", "mails", "todos", "digests", "history", "exports"] as const;

/**
 * Deleting a user with all their data (Art. 17), confirmed by typing the user's address.
 * Refusals (last admin, admin lockout) are shown in the dialog, not as a toast.
 */
export function DeleteUserDialog({
  user,
  isSelf,
  onOpenChange,
  onDeleted,
}: {
  user: AdminUser | undefined;
  isSelf: boolean;
  onOpenChange: (open: boolean) => void;
  onDeleted: (result: UserDeletionResult, user: AdminUser) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const remove = useDeleteUser();
  const [confirmation, setConfirmation] = useState("");
  const matches =
    user !== undefined && confirmation.trim().toLowerCase() === user.email.toLowerCase();

  const close = (open: boolean) => {
    if (open) return;
    setConfirmation("");
    remove.reset();
    onOpenChange(false);
  };

  let error: string | undefined;
  if (remove.isError) {
    if (isLastAdmin(remove.error)) error = t("pages.users.delete.lastAdmin");
    else if (isAdminLockout(remove.error)) error = t("pages.users.delete.lockout");
    else error = describeApiError(remove.error, t).title;
  }

  return (
    <Dialog open={user !== undefined} onOpenChange={close}>
      <DialogContent
        closeLabel={t("common.close")}
        // Long on phones (list + own-account warning + error): scroll instead of overflowing.
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle className="text-base">
            {isSelf ? t("pages.users.delete.selfTitle") : t("pages.users.delete.title")}
          </DialogTitle>
          <DialogDescription className="text-ui">
            {t("pages.users.delete.description", {
              name: user?.display_name,
              email: user?.email,
            })}
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-0.5 pl-5 text-ui">
          {deletedItems.map((item) => (
            <li key={item}>{t(`pages.users.delete.items.${item}`)}</li>
          ))}
        </ul>
        <p className="text-ui text-muted-foreground">{t("pages.users.delete.kept")}</p>
        {isSelf && <Notice tone="warning">{t("pages.users.delete.selfWarning")}</Notice>}
        <form
          id={`${id}-form`}
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!user || !matches) return;
            remove.mutate(user.id, {
              onSuccess: (result) => {
                setConfirmation("");
                onDeleted(result, user);
              },
            });
          }}
        >
          <Label htmlFor={`${id}-email`} className="text-ui">
            {t("pages.users.delete.confirmLabel", { email: user?.email })}
          </Label>
          <Input
            id={`${id}-email`}
            type="email"
            autoComplete="off"
            spellCheck={false}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
        </form>
        {error && <Notice tone="error">{error}</Notice>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">{t("pages.users.delete.cancel")}</Button>
          </DialogClose>
          <Button
            type="submit"
            form={`${id}-form`}
            variant="destructive"
            disabled={!matches || remove.isPending}
          >
            {t("pages.users.delete.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
