import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { pageNavigation } from "@/api/auth";
import { isApiError } from "@/api/errors";
import { accountPrivacyQueryOptions, useDeleteAccount } from "@/api/privacy";
import { isReauthCancelled } from "@/api/reauth";
import { InlineError } from "@/components/inline-error";
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
import { useCurrentUser } from "@/hooks/use-current-user";

/** Deleting the own account (Art. 17), confirmed by typing the account's address. */
export function DeleteAccountSection() {
  const { t } = useTranslation();
  const id = useId();
  const user = useCurrentUser();
  const options = useQuery(accountPrivacyQueryOptions);
  const remove = useDeleteAccount();
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const matches = confirmation.trim().toLowerCase() === user.email;
  const enabled = options.data?.self_delete_enabled ?? false;

  const onOpenChange = (value: boolean) => {
    setOpen(value);
    if (!value) {
      setConfirmation("");
      remove.reset();
    }
  };

  return (
    <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <div className="text-ui font-medium">{t("privacy.delete.title")}</div>
        <div className="text-ui text-muted-foreground">
          {options.isPending || enabled
            ? t("privacy.delete.description")
            : t("privacy.delete.disabled")}
        </div>
      </div>
      {enabled && (
        <Button
          variant="outline"
          size="sm"
          className="shrink-0 self-start text-ui text-destructive hover:text-destructive sm:self-auto"
          onClick={() => setOpen(true)}
        >
          {t("privacy.delete.open")}
        </Button>
      )}
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent closeLabel={t("common.close")}>
          <DialogHeader>
            <DialogTitle>{t("privacy.delete.confirmTitle")}</DialogTitle>
            <DialogDescription>{t("privacy.delete.confirmDescription")}</DialogDescription>
          </DialogHeader>
          <form
            id={`${id}-form`}
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!matches) return;
              remove.mutate(confirmation.trim(), {
                onSuccess: () => pageNavigation.assign("/login"),
              });
            }}
          >
            <Label htmlFor={`${id}-email`} className="text-ui">
              {t("privacy.delete.confirmLabel", { email: user.email })}
            </Label>
            <Input
              id={`${id}-email`}
              type="email"
              autoComplete="off"
              spellCheck={false}
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
            {remove.isError &&
              !isReauthCancelled(remove.error) &&
              (isApiError(remove.error) && remove.error.status === 409 ? (
                <p role="alert" className="text-ui text-destructive">
                  {t("privacy.delete.lastAdmin")}
                </p>
              ) : (
                <InlineError error={remove.error} />
              ))}
          </form>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">{t("privacy.delete.cancel")}</Button>
            </DialogClose>
            <Button
              type="submit"
              form={`${id}-form`}
              variant="destructive"
              disabled={!matches || remove.isPending}
            >
              {t("privacy.delete.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
