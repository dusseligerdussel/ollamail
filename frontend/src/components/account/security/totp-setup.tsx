import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { describeApiError, isApiError } from "@/api/errors";
import { confirmTotp, isMfaExpired, type MfaEnrolled, setupTotp } from "@/api/mfa";
import { CopyField } from "@/components/copy-field";
import { FormField } from "@/components/form-field";
import { InlineError } from "@/components/inline-error";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Set up an authenticator app: QR code (rendered by the server), the key for manual entry
 * and the confirmation code. `duringLogin`: the enforced set-up before the first session.
 */
export function TotpSetup({
  duringLogin = false,
  onEnrolled,
  onExpired,
}: {
  duringLogin?: boolean;
  onEnrolled: (result: MfaEnrolled) => void;
  onExpired?: () => void;
}) {
  const { t } = useTranslation();
  const [code, setCode] = useState("");
  const setup = useMutation({
    mutationFn: () => setupTotp(duringLogin),
    meta: { errorToast: false },
  });
  const confirm = useMutation({
    mutationFn: (value: string) => confirmTotp(value, duringLogin),
    meta: { errorToast: false },
    onSuccess: onEnrolled,
    onError: (error) => {
      if (isMfaExpired(error)) onExpired?.();
    },
  });

  // One secret per opened form (the effect runs twice in development).
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    setup.mutate();
  }, [setup]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (code.trim()) confirm.mutate(code.trim());
  }

  let error: string | undefined;
  if (confirm.isError) {
    const status = isApiError(confirm.error) ? confirm.error.status : 0;
    if (status === 400) error = t("account.security.totp.invalid");
    else if (isMfaExpired(confirm.error)) error = t("auth.mfa.expired");
    else if (status === 429) error = t("auth.login.throttled");
    else error = describeApiError(confirm.error, t).title;
  }

  if (setup.isError) {
    return (
      <InlineError error={setup.error} onRetry={() => setup.mutate()} retrying={setup.isPending} />
    );
  }

  return (
    <form noValidate onSubmit={submit} className="flex flex-col gap-5">
      <div className="flex flex-col gap-3">
        <p className="text-ui">{t("account.security.totp.scan")}</p>
        {setup.data ? (
          <img
            src={setup.data.qr_svg}
            alt={t("account.security.totp.qrAlt")}
            width={176}
            height={176}
            className="size-44 self-center rounded-md border bg-white sm:self-start"
          />
        ) : (
          <Skeleton
            role="status"
            aria-label={t("common.loading")}
            className="size-44 self-center sm:self-start"
          />
        )}
        {setup.data && (
          <CopyField label={t("account.security.totp.secret")} value={setup.data.secret} />
        )}
      </div>
      <div className="flex flex-col gap-3">
        <p className="text-ui">{t("account.security.totp.enterCode")}</p>
        <FormField
          label={t("account.security.totp.code")}
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={8}
          className="w-40 font-mono tracking-widest"
          value={code}
          onChange={(event) => setCode(event.target.value)}
          error={error}
          disabled={!setup.data}
        />
      </div>
      <Button
        type="submit"
        className="self-start"
        disabled={!setup.data || !code.trim() || confirm.isPending}
      >
        {confirm.isPending
          ? t("account.security.totp.confirming")
          : t("account.security.totp.confirm")}
      </Button>
    </form>
  );
}
