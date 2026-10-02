import { useMutation } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import type { User } from "@/api/auth";
import { describeApiError, isApiError } from "@/api/errors";
import {
  cancelMfa,
  isMfaExpired,
  type MfaChallenge,
  type MfaEnrolled,
  type MfaMethod,
  verifyCode,
  verifyPasskey,
} from "@/api/mfa";
import { PasskeyForm } from "@/components/account/security/passkey-form";
import { RecoveryCodeList } from "@/components/account/security/recovery-code-list";
import { TotpSetup } from "@/components/account/security/totp-setup";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { isWebauthnAbort, webauthnSupported } from "@/lib/webauthn";

function usableMethods(challenge: MfaChallenge): MfaMethod[] {
  return challenge.methods.filter((method) => method !== "webauthn" || webauthnSupported());
}

function stepError(error: unknown, t: TFunction, passkey: boolean) {
  if (isWebauthnAbort(error)) return t("auth.mfa.passkeyCancelled");
  if (!isApiError(error)) return t("auth.mfa.passkeyFailed");
  if (error.status === 429) return t("auth.login.throttled");
  if (error.status === 401)
    return passkey ? t("auth.mfa.passkeyFailed") : t("auth.mfa.invalidCode");
  return describeApiError(error, t).title;
}

function StepHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="flex flex-col gap-1">
      <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
      <p className="text-ui text-muted-foreground">{description}</p>
    </div>
  );
}

/** Links to the other methods, below the current one. */
function MethodLinks<M extends string>({
  methods,
  current,
  label,
  onSelect,
}: {
  methods: M[];
  current: M;
  label: (method: M) => string;
  onSelect: (method: M) => void;
}) {
  const { t } = useTranslation();
  const others = methods.filter((method) => method !== current);
  if (others.length === 0) return null;
  return (
    <nav aria-label={t("auth.mfa.otherMethods")} className="flex flex-col items-start gap-1">
      {others.map((method) => (
        <Button
          key={method}
          type="button"
          variant="link"
          className="h-auto p-0 text-ui"
          onClick={() => onSelect(method)}
        >
          {label(method)}
        </Button>
      ))}
    </nav>
  );
}

function BackLink({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="self-start px-0 text-ui text-muted-foreground hover:bg-transparent"
      onClick={() => {
        // Best effort: the pending sign-in also expires on its own.
        void cancelMfa().catch(() => {});
        onBack();
      }}
    >
      {t("auth.mfa.back")}
    </Button>
  );
}

/** Second step of the sign-in: passkey, authenticator code or recovery code. */
export function SecondFactorStep({
  challenge,
  onSignedIn,
  onExpired,
  onBack,
}: {
  challenge: MfaChallenge;
  onSignedIn: (user: User) => void;
  onExpired: () => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const methods = usableMethods(challenge);
  const [method, setMethod] = useState<MfaMethod | undefined>(methods[0]);
  const [code, setCode] = useState("");
  const verify = useMutation({
    mutationFn: () =>
      method === "webauthn" ? verifyPasskey() : verifyCode(method ?? "totp", code.trim()),
    meta: { errorToast: false },
    onSuccess: onSignedIn,
    onError: (error) => {
      if (isMfaExpired(error)) onExpired();
    },
  });

  function select(next: MfaMethod) {
    verify.reset();
    setCode("");
    setMethod(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (method === "webauthn" || code.trim()) verify.mutate();
  }

  const error = verify.isError ? stepError(verify.error, t, method === "webauthn") : undefined;

  return (
    <div className="flex flex-col gap-6">
      <StepHeader title={t("auth.mfa.title")} description={t("auth.mfa.description")} />
      {method === undefined ? (
        <FormError>{t("auth.mfa.noMethods")}</FormError>
      ) : (
        <form noValidate onSubmit={submit} className="flex flex-col gap-4">
          {method === "webauthn" ? (
            <p className="text-ui text-muted-foreground">{t("auth.mfa.passkeyHint")}</p>
          ) : (
            <FormField
              key={method}
              label={t(method === "totp" ? "auth.mfa.totpLabel" : "auth.mfa.recoveryLabel")}
              description={t(method === "totp" ? "auth.mfa.totpHint" : "auth.mfa.recoveryHint")}
              name="code"
              autoFocus
              autoComplete="one-time-code"
              inputMode={method === "totp" ? "numeric" : "text"}
              maxLength={method === "totp" ? 8 : 16}
              className="font-mono tracking-widest"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              aria-invalid={error ? true : undefined}
            />
          )}
          {error && <FormError>{error}</FormError>}
          <Button type="submit" disabled={verify.isPending} className="mt-1">
            {verify.isPending
              ? t(method === "webauthn" ? "auth.mfa.waiting" : "auth.mfa.verifying")
              : t(method === "webauthn" ? "auth.mfa.usePasskey" : "auth.mfa.verify")}
          </Button>
        </form>
      )}
      {method && (
        <MethodLinks
          methods={methods}
          current={method}
          label={(m) => t(`auth.mfa.switch.${m}`)}
          onSelect={select}
        />
      )}
      <BackLink onBack={onBack} />
    </div>
  );
}

/** Enforced set-up of a first factor before the first session; then the recovery codes. */
export function EnrollStep({
  challenge,
  onSignedIn,
  onExpired,
  onBack,
}: {
  challenge: MfaChallenge;
  onSignedIn: (user: User) => void;
  onExpired: () => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const methods = usableMethods(challenge).filter(
    (m): m is "webauthn" | "totp" => m === "webauthn" || m === "totp",
  );
  // The authenticator app works everywhere; a passkey is one click away.
  const [method, setMethod] = useState<"webauthn" | "totp">(
    methods.includes("totp") ? "totp" : (methods[0] ?? "totp"),
  );
  const [done, setDone] = useState<MfaEnrolled>();

  if (done?.user) {
    const user = done.user;
    return (
      <div className="flex flex-col gap-6">
        <StepHeader
          title={t("auth.mfa.codesTitle")}
          description={t("account.security.recovery.sheetDescription")}
        />
        {done.recovery_codes && <RecoveryCodeList codes={done.recovery_codes} />}
        <Button onClick={() => onSignedIn(user)}>{t("auth.mfa.continue")}</Button>
      </div>
    );
  }

  const enrolled = (result: MfaEnrolled) => {
    if (result.user && !result.recovery_codes) onSignedIn(result.user);
    else setDone(result);
  };

  return (
    <div className="flex flex-col gap-6">
      <StepHeader title={t("auth.mfa.enrollTitle")} description={t("auth.mfa.enrollDescription")} />
      {method === "totp" ? (
        <TotpSetup duringLogin onEnrolled={enrolled} onExpired={onExpired} />
      ) : (
        <PasskeyForm
          duringLogin
          defaultName={t("auth.mfa.passkeyName")}
          onEnrolled={enrolled}
          onExpired={onExpired}
        />
      )}
      <MethodLinks
        methods={methods}
        current={method}
        label={(m) => t(`auth.mfa.enrollSwitch.${m}`)}
        onSelect={setMethod}
      />
      <BackLink onBack={onBack} />
    </div>
  );
}
