import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  adminUsersQueryOptions,
  type InvitationIssued,
  inviteUser,
  type Role,
  roles,
} from "@/api/admin-auth";
import { describeApiError, isApiError } from "@/api/errors";
import { isReauthCancelled } from "@/api/reauth";
import { Notice } from "@/components/admin/notice";
import { useReauth } from "@/components/auth/reauth";
import { CopyField } from "@/components/copy-field";
import { FormField } from "@/components/form-field";
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
import { type SupportedLanguage, supportedLanguages } from "@/i18n";
import { browserTimeZone } from "@/lib/time-zones";

/** Shows a freshly created invitation link (also used for "new invitation link"). */
export function InvitationLink({ issued }: { issued: InvitationIssued }) {
  const { t, i18n } = useTranslation();
  const date = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(issued.expires_at));
  return (
    <CopyField
      label={t("pages.users.inviteSheet.linkTitle")}
      value={issued.invite_url}
      description={t("pages.users.inviteSheet.linkDescription", { date })}
    />
  );
}

export function InviteUserSheet({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 sm:max-w-md" closeLabel={t("common.close")}>
        <SheetHeader className="border-b">
          <SheetTitle>{t("pages.users.inviteSheet.title")}</SheetTitle>
          <SheetDescription className="text-ui">
            {t("pages.users.inviteSheet.description")}
          </SheetDescription>
        </SheetHeader>
        {open && <InviteForm onDone={() => onOpenChange(false)} />}
      </SheetContent>
    </Sheet>
  );
}

function InviteForm({ onDone }: { onDone: () => void }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const id = useId();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [language, setLanguage] = useState<SupportedLanguage>(
    (i18n.resolvedLanguage as SupportedLanguage | undefined) ?? "en",
  );
  const withReauth = useReauth();
  const invite = useMutation({
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: () =>
      withReauth(() =>
        inviteUser({ email, display_name: name, role, language, timezone: browserTimeZone() }),
      ),
    meta: { errorToast: false },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: adminUsersQueryOptions.queryKey }),
  });

  if (invite.data) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex flex-col gap-4 overflow-y-auto p-4">
          <InvitationLink issued={invite.data} />
        </div>
        <SheetFooter className="flex-row justify-end border-t">
          <Button onClick={onDone}>{t("pages.users.inviteSheet.done")}</Button>
        </SheetFooter>
      </div>
    );
  }

  let error: string | undefined;
  if (invite.isError && !isReauthCancelled(invite.error)) {
    const type = isApiError(invite.error) ? invite.error.problem?.type : undefined;
    error =
      type === "urn:ollamail:problem:local-login-disabled"
        ? t("pages.users.inviteSheet.localDisabled")
        : describeApiError(invite.error, t).title;
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    invite.mutate();
  }

  return (
    <form onSubmit={onSubmit} className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-4 overflow-y-auto p-4">
        <FormField
          label={t("auth.fields.email")}
          type="email"
          required
          autoComplete="off"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <FormField
          label={t("auth.fields.displayName")}
          required
          autoComplete="off"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-2">
            <Label htmlFor={`${id}-role`} className="text-ui">
              {t("pages.roleMapping.role")}
            </Label>
            <NativeSelect
              id={`${id}-role`}
              className="w-full"
              value={role}
              onChange={(event) => setRole(event.target.value as Role)}
            >
              {roles.map((value) => (
                <NativeSelectOption key={value} value={value}>
                  {t(`account.roles.${value}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor={`${id}-language`} className="text-ui">
              {t("pages.users.inviteSheet.language")}
            </Label>
            <NativeSelect
              id={`${id}-language`}
              className="w-full"
              value={language}
              onChange={(event) => setLanguage(event.target.value as SupportedLanguage)}
            >
              {supportedLanguages.map((value) => (
                <NativeSelectOption key={value} value={value}>
                  {t(`language.${value}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
      </div>
      <SheetFooter className="flex-row justify-end border-t">
        <Button type="submit" disabled={invite.isPending}>
          {invite.isPending
            ? t("pages.users.inviteSheet.submitting")
            : t("pages.users.inviteSheet.submit")}
        </Button>
      </SheetFooter>
    </form>
  );
}
