import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { createFirstAdmin, setSignedIn, type User } from "@/api/auth";
import { describeApiError, isApiError } from "@/api/errors";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { useDocumentTitle } from "@/hooks/use-document-title";
import { supportedLanguages } from "@/i18n";
import { browserTimeZone } from "@/lib/time-zones";

export const Route = createFileRoute("/setup")({
  staticData: { public: true },
  component: SetupPage,
});

const PASSWORD_TOO_SHORT = "urn:ollamail:problem:password-too-short";
const ALREADY_INITIALIZED = "urn:ollamail:problem:already-initialized";

type FieldErrors = Partial<Record<"password" | "setupToken", string>>;

function SetupPage() {
  const [admin, setAdmin] = useState<User | null>(null);
  return admin ? <SetupDone admin={admin} /> : <SetupForm onDone={setAdmin} />;
}

function SetupForm({ onDone }: { onDone: (admin: User) => void }) {
  const { t, i18n } = useTranslation();
  useDocumentTitle(t("auth.setup.title"));
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [setupToken, setSetupToken] = useState("");

  const setup = useMutation({
    mutationFn: createFirstAdmin,
    meta: { errorToast: false },
    onSuccess: (user) => {
      setSignedIn(queryClient, user);
      onDone(user);
    },
  });

  const fieldErrors: FieldErrors = {};
  let formError: string | undefined;
  if (setup.isError) {
    const error = setup.error;
    const type = isApiError(error) ? error.problem?.type : undefined;
    if (isApiError(error) && error.status === 403) {
      fieldErrors.setupToken = t("auth.setup.invalidToken");
    } else if (type === PASSWORD_TOO_SHORT) {
      const minLength = isApiError(error) ? Number(error.problem?.min_length) || 12 : 12;
      fieldErrors.password = t("auth.passwordTooShort", { count: minLength });
    } else if (type === ALREADY_INITIALIZED) {
      formError = t("auth.setup.alreadyInitialized");
    } else {
      formError = describeApiError(error, t).title;
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const language = i18n.resolvedLanguage;
    setup.mutate({
      display_name: displayName,
      email,
      password,
      setup_token: setupToken,
      language: (supportedLanguages as readonly string[]).includes(language ?? "")
        ? (language as (typeof supportedLanguages)[number])
        : "en",
      timezone: browserTimeZone(),
    });
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">{t("auth.setup.step", { step: 1, of: 2 })}</p>
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.setup.title")}</h1>
        <p className="text-ui text-muted-foreground">{t("auth.setup.description")}</p>
      </div>
      <form className="flex flex-col gap-4" onSubmit={onSubmit}>
        <FormField
          label={t("auth.fields.displayName")}
          name="display_name"
          autoComplete="name"
          required
          maxLength={255}
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
        />
        <FormField
          label={t("auth.fields.email")}
          type="email"
          name="email"
          autoComplete="email"
          required
          maxLength={320}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <FormField
          label={t("auth.fields.password")}
          type="password"
          name="password"
          autoComplete="new-password"
          required
          description={t("auth.setup.passwordHint")}
          error={fieldErrors.password}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <FormField
          label={t("auth.fields.setupToken")}
          name="setup_token"
          autoComplete="off"
          spellCheck={false}
          required
          maxLength={256}
          description={t("auth.setup.tokenHint")}
          error={fieldErrors.setupToken}
          value={setupToken}
          onChange={(event) => setSetupToken(event.target.value)}
        />
        {formError && <FormError>{formError}</FormError>}
        <Button type="submit" disabled={setup.isPending} className="mt-1">
          {setup.isPending ? t("auth.setup.submitting") : t("auth.setup.submit")}
        </Button>
      </form>
    </div>
  );
}

function SetupDone({ admin }: { admin: User }) {
  const { t } = useTranslation();
  useDocumentTitle(t("auth.setup.doneTitle"));
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">{t("auth.setup.step", { step: 2, of: 2 })}</p>
        <h1 className="text-lg font-semibold tracking-tight">{t("auth.setup.doneTitle")}</h1>
        <p className="text-ui text-muted-foreground">
          {t("auth.setup.doneDescription", { email: admin.email })}
        </p>
      </div>
      <div className="flex flex-col gap-3 rounded-lg border p-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-ui font-medium">{t("auth.setup.providersTitle")}</h2>
          <p className="text-ui text-muted-foreground">{t("auth.setup.providersDescription")}</p>
        </div>
        <Button asChild variant="outline" size="sm" className="self-start">
          <Link to="/admin">{t("auth.setup.providersAction")}</Link>
        </Button>
      </div>
      <Button asChild>
        <Link to="/inbox">{t("auth.setup.continue")}</Link>
      </Button>
    </div>
  );
}
