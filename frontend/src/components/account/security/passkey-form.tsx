import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { describeApiError, isApiError } from "@/api/errors";
import { addPasskey, isMfaExpired, type MfaEnrolled } from "@/api/mfa";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { isWebauthnAbort } from "@/lib/webauthn";

const PASSKEY_EXISTS = "urn:ollamail:problem:passkey-exists";

/** Name a new passkey and run the browser dialog. */
export function PasskeyForm({
  duringLogin = false,
  defaultName = "",
  onEnrolled,
  onExpired,
}: {
  duringLogin?: boolean;
  defaultName?: string;
  onEnrolled: (result: MfaEnrolled) => void;
  onExpired?: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(defaultName);
  const [submitted, setSubmitted] = useState(false);
  const add = useMutation({
    mutationFn: (value: string) => addPasskey(value, duringLogin),
    meta: { errorToast: false },
    onSuccess: onEnrolled,
    onError: (error) => {
      if (isMfaExpired(error)) onExpired?.();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitted(true);
    if (name.trim()) add.mutate(name.trim());
  }

  let error: string | undefined;
  if (add.isError) {
    if (isWebauthnAbort(add.error)) error = t("account.security.passkeys.cancelled");
    else if (isApiError(add.error) && add.error.problem?.type === PASSKEY_EXISTS)
      error = t("account.security.passkeys.exists");
    else if (isApiError(add.error) && add.error.status === 400)
      error = t("account.security.passkeys.failed");
    else if (isApiError(add.error)) error = describeApiError(add.error, t).title;
    else error = t("account.security.passkeys.failed");
  }

  return (
    <form noValidate onSubmit={submit} className="flex flex-col gap-4">
      <FormField
        label={t("account.security.passkeys.name")}
        name="passkey-name"
        placeholder={t("account.security.passkeys.namePlaceholder")}
        maxLength={64}
        autoComplete="off"
        value={name}
        onChange={(event) => setName(event.target.value)}
        error={submitted && !name.trim() ? t("account.security.passkeys.nameRequired") : undefined}
      />
      {error && <FormError>{error}</FormError>}
      <Button type="submit" className="self-start" disabled={add.isPending}>
        {add.isPending ? t("auth.mfa.waiting") : t("account.security.passkeys.create")}
      </Button>
    </form>
  );
}
