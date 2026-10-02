import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { Fingerprint, LogIn } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { authProvidersQueryOptions, login, safeRedirect, setSignedIn, type User } from "@/api/auth";
import { describeApiError, isApiError } from "@/api/errors";
import { type MfaChallenge, signInWithPasskey } from "@/api/mfa";
import { EnrollStep, SecondFactorStep } from "@/components/auth/second-factor";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { supportedLanguages } from "@/i18n";
import { isWebauthnAbort, webauthnSupported } from "@/lib/webauthn";

interface LoginSearch {
  redirect?: string;
}

export const Route = createFileRoute("/login")({
  staticData: { public: true },
  validateSearch: (search: Record<string, unknown>): LoginSearch =>
    typeof search.redirect === "string" ? { redirect: search.redirect } : {},
  loader: ({ context: { queryClient } }) => {
    // Not awaited: the local form does not depend on it, provider buttons appear when loaded.
    void queryClient.prefetchQuery(authProvidersQueryOptions);
  },
  component: LoginPage,
});

/**
 * Start URL of a redirect provider (OIDC, GitHub): a full page navigation to the backend
 * (`login_path` below `/api`), which sends the browser to the identity provider and back.
 */
export function providerLoginUrl(loginPath: string, redirect: string) {
  const params = new URLSearchParams({ return_to: redirect });
  return `/api${loginPath}?${params}`;
}

function loginErrorMessage(error: unknown, t: TFunction) {
  if (isApiError(error) && error.status === 401) return t("auth.login.invalidCredentials");
  if (isApiError(error) && error.status === 429) return t("auth.login.throttled");
  return describeApiError(error, t).title;
}

function passkeyErrorMessage(error: unknown, t: TFunction) {
  if (isWebauthnAbort(error)) return t("auth.login.passkeyCancelled");
  if (isApiError(error) && error.status === 401) return t("auth.login.passkeyFailed");
  if (isApiError(error) && error.status === 429) return t("auth.login.throttled");
  if (isApiError(error)) return describeApiError(error, t).title;
  return t("auth.login.passkeyFailed");
}

function isMfaChallenge(result: User | MfaChallenge): result is MfaChallenge {
  return "status" in result && "methods" in result;
}

function LoginPage() {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const search = Route.useSearch();
  const providers = useQuery(authProvidersQueryOptions);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  // Set when the password was right but a second step is needed (no session yet).
  const [challenge, setChallenge] = useState<MfaChallenge>();
  const [expired, setExpired] = useState(false);

  const target = safeRedirect(search.redirect);
  const signedIn = async (user: User) => {
    setSignedIn(queryClient, user);
    if ((supportedLanguages as readonly string[]).includes(user.language)) {
      await i18n.changeLanguage(user.language);
    }
    await navigate({ href: target, replace: true });
  };
  const signIn = useMutation({
    mutationFn: login,
    meta: { errorToast: false },
    onSuccess: async (result) => {
      if (isMfaChallenge(result)) {
        setPassword("");
        setChallenge(result);
      } else {
        await signedIn(result);
      }
    },
  });
  const passkeySignIn = useMutation({
    mutationFn: signInWithPasskey,
    meta: { errorToast: false },
    onSuccess: signedIn,
  });

  // The backend always offers the local login today; assume it until the list has loaded.
  const localLogin = providers.data?.local_login ?? true;
  const externalProviders = (providers.data?.providers ?? []).flatMap((p) =>
    p.kind === "redirect" && p.login_path ? [{ ...p, login_path: p.login_path }] : [],
  );

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setExpired(false);
    passkeySignIn.reset();
    signIn.mutate({ email, password });
  }

  function restart(afterExpiry: boolean) {
    signIn.reset();
    setChallenge(undefined);
    setExpired(afterExpiry);
  }

  if (challenge) {
    const Step = challenge.status === "mfa_enrollment_required" ? EnrollStep : SecondFactorStep;
    return (
      <Step
        challenge={challenge}
        onSignedIn={signedIn}
        onExpired={() => restart(true)}
        onBack={() => restart(false)}
      />
    );
  }

  const passkeyLogin = localLogin && !!providers.data?.passkey_login && webauthnSupported();

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.login.title")}</h1>
        <p className="text-ui text-muted-foreground">{t("auth.login.description")}</p>
      </div>

      {localLogin && (
        <form className="flex flex-col gap-4" onSubmit={onSubmit}>
          <FormField
            label={t("auth.fields.email")}
            type="email"
            name="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <FormField
            label={t("auth.fields.password")}
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {expired && <FormError>{t("auth.mfa.expired")}</FormError>}
          {signIn.isError && <FormError>{loginErrorMessage(signIn.error, t)}</FormError>}
          <Button type="submit" disabled={signIn.isPending} className="mt-1">
            {signIn.isPending ? t("auth.login.submitting") : t("auth.login.submit")}
          </Button>
        </form>
      )}

      {(externalProviders.length > 0 || passkeyLogin) && (
        <div className="flex flex-col gap-3">
          {localLogin && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              {t("auth.login.or")}
              <span className="h-px flex-1 bg-border" />
            </div>
          )}
          <ul className="flex flex-col gap-2" aria-label={t("auth.login.providers")}>
            {passkeyLogin && (
              <li>
                <Button
                  variant="outline"
                  className="w-full"
                  disabled={passkeySignIn.isPending}
                  onClick={() => {
                    setExpired(false);
                    signIn.reset();
                    passkeySignIn.mutate();
                  }}
                >
                  <Fingerprint aria-hidden="true" />
                  {passkeySignIn.isPending ? t("auth.mfa.waiting") : t("auth.login.passkey")}
                </Button>
              </li>
            )}
            {externalProviders.map((provider) => (
              <li key={provider.name}>
                <Button asChild variant="outline" className="w-full">
                  <a href={providerLoginUrl(provider.login_path, target)}>
                    <LogIn aria-hidden="true" />
                    {t("auth.login.withProvider", { provider: provider.display_name })}
                  </a>
                </Button>
              </li>
            ))}
          </ul>
          {passkeySignIn.isError && (
            <FormError>{passkeyErrorMessage(passkeySignIn.error, t)}</FormError>
          )}
        </div>
      )}

      {!localLogin && externalProviders.length === 0 && providers.isSuccess && (
        <FormError>{t("auth.login.noMethods")}</FormError>
      )}
    </div>
  );
}
