import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { acceptInvitation, lookupInvitation } from "@/api/admin-auth";
import { setSignedIn } from "@/api/auth";
import { describeApiError, isApiError } from "@/api/errors";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { supportedLanguages } from "@/i18n";

/**
 * "Set your password" page of an invitation link (`/invite#<token>`). The token stays in the
 * URL fragment, so it never reaches server logs; it is sent in POST bodies only.
 */
export const Route = createFileRoute("/invite")({
  staticData: { public: true },
  component: InvitePage,
});

const PASSWORD_TOO_SHORT = "urn:ollamail:problem:password-too-short";

function InvitePage() {
  const { t } = useTranslation();
  const [token] = useState(() => window.location.hash.slice(1));
  const invitation = useQuery({
    queryKey: ["invitation", token],
    queryFn: () => lookupInvitation(token),
    enabled: token.length > 0,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    meta: { errorToast: false },
  });

  if (token && invitation.isPending) {
    return (
      <h1 className="text-lg font-semibold tracking-tight" aria-busy="true">
        {t("auth.invite.loading")}
      </h1>
    );
  }
  if (!token || !invitation.data) {
    const invalid = !token || (isApiError(invitation.error) && invitation.error.status === 404);
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.invite.title")}</h1>
        <FormError>
          {invalid ? t("auth.invite.invalid") : describeApiError(invitation.error, t).title}
        </FormError>
        <Button asChild variant="outline">
          <Link to="/login">{t("auth.invite.toLogin")}</Link>
        </Button>
      </div>
    );
  }
  return <SetPasswordForm token={token} email={invitation.data.email} />;
}

function SetPasswordForm({ token, email }: { token: string; email: string }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const accept = useMutation({
    mutationFn: () => acceptInvitation(token, password),
    meta: { errorToast: false },
    onSuccess: async (user) => {
      // Drop the token from the address bar and history.
      window.history.replaceState(null, "", window.location.pathname);
      setSignedIn(queryClient, user);
      if ((supportedLanguages as readonly string[]).includes(user.language)) {
        await i18n.changeLanguage(user.language);
      }
      await navigate({ to: "/inbox", replace: true });
    },
  });

  let passwordError: string | undefined;
  let formError: string | undefined;
  if (mismatch) passwordError = t("auth.invite.mismatch");
  else if (accept.isError) {
    const error = accept.error;
    if (isApiError(error) && error.problem?.type === PASSWORD_TOO_SHORT) {
      passwordError = t("auth.passwordTooShort", {
        count: Number(error.problem?.min_length) || 12,
      });
    } else if (isApiError(error) && error.status === 404) {
      formError = t("auth.invite.invalid");
    } else {
      formError = describeApiError(error, t).title;
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const different = password !== confirm;
    setMismatch(different);
    if (!different) accept.mutate();
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.invite.title")}</h1>
        <p className="text-ui text-muted-foreground">{t("auth.invite.description", { email })}</p>
      </div>
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        {/* Lets password managers store the credentials under the right account. */}
        <input type="email" name="email" autoComplete="username" value={email} readOnly hidden />
        <FormField
          label={t("auth.fields.password")}
          type="password"
          autoComplete="new-password"
          required
          value={password}
          error={passwordError}
          onChange={(event) => setPassword(event.target.value)}
        />
        <FormField
          label={t("auth.invite.confirm")}
          type="password"
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
        />
        {formError && <FormError>{formError}</FormError>}
        <Button type="submit" disabled={accept.isPending} className="mt-1">
          {accept.isPending ? t("auth.invite.submitting") : t("auth.invite.submit")}
        </Button>
      </form>
    </div>
  );
}
