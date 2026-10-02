import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, CircleCheck, ExternalLink } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { isApiError } from "@/api/errors";
import {
  type AutodiscoverSuggestion,
  autodiscover,
  type ConnectionTestResult,
  createMailbox,
  type MailboxConnection,
  type MailboxProvider,
  type MailboxType,
  mailboxProvidersQueryOptions,
  problemErrorCode,
  startOAuthConnect,
  testConnection,
} from "@/api/mail";
import { FormError, FormField } from "@/components/form-field";
import { InlineError } from "@/components/inline-error";
import { useMailErrorText } from "@/components/mail/sync-status";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";

export const Route = createFileRoute("/settings_/mailboxes_/new")({
  component: NewMailboxPage,
});

const RETURN_PATH = "/settings/mailboxes";
const ADDRESS = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

function NewMailboxPage() {
  const { t } = useTranslation();
  const providers = useQuery(mailboxProvidersQueryOptions);
  const [selected, setSelected] = useState<MailboxType>();
  const type = selected ?? providers.data?.[0]?.type;
  const provider = providers.data?.find((item) => item.type === type);

  return (
    <>
      <PageHeader
        title={t("mailboxes.addTitle")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings/mailboxes" aria-label={t("mailboxes.backToList")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {providers.isPending && <Skeleton className="h-24 w-full" />}
          {providers.isError && <InlineError error={providers.error} />}
          {providers.data && providers.data.length === 0 && (
            <p className="text-ui text-muted-foreground">{t("mailboxes.noProviders")}</p>
          )}
          {providers.data && providers.data.length > 1 && (
            <ProviderChoice providers={providers.data} value={type} onChange={setSelected} />
          )}
          {provider?.connect === "credentials" && (
            <ImapForm
              onPreferOAuth={(native) =>
                providers.data?.some((item) => item.type === native) && setSelected(native)
              }
              oauthTypes={providers.data?.filter((p) => p.connect === "oauth").map((p) => p.type)}
            />
          )}
          {provider?.connect === "oauth" && <OAuthConnect provider={provider} />}
        </div>
      </div>
    </>
  );
}

function ProviderChoice({
  providers,
  value,
  onChange,
}: {
  providers: MailboxProvider[];
  value: MailboxType | undefined;
  onChange: (type: MailboxType) => void;
}) {
  const { t } = useTranslation();
  const labelId = useId();
  return (
    <section aria-labelledby={labelId} className="mb-8">
      <h2 id={labelId} className="mb-2 text-xs font-medium text-muted-foreground">
        {t("mailboxes.providerLabel")}
      </h2>
      <RadioGroup
        aria-labelledby={labelId}
        value={value}
        onValueChange={(next) => onChange(next as MailboxType)}
        className="gap-0 divide-y rounded-lg border"
      >
        {providers.map((provider) => {
          const id = `${labelId}-${provider.type}`;
          return (
            <div key={provider.type} className="flex items-start gap-3 px-4 py-3">
              <RadioGroupItem id={id} value={provider.type} className="mt-0.5" />
              <Label htmlFor={id} className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                <span className="text-ui font-medium">
                  {t(`mailboxes.providers.${provider.type}.title`)}
                </span>
                <span className="text-ui font-normal text-muted-foreground">
                  {t(`mailboxes.providers.${provider.type}.description`)}
                </span>
              </Label>
            </div>
          );
        })}
      </RadioGroup>
    </section>
  );
}

function OAuthConnect({ provider }: { provider: MailboxProvider }) {
  const { t } = useTranslation();
  const connect = useMutation({
    mutationFn: () => startOAuthConnect(provider.oauth_start_path ?? "", RETURN_PATH),
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  return (
    <section className="rounded-lg border px-4 py-4">
      <p className="text-ui text-muted-foreground">
        {t(`mailboxes.providers.${provider.type}.connectHint`)}
      </p>
      <Button
        className="mt-4"
        size="sm"
        disabled={connect.isPending}
        onClick={() => connect.mutate()}
      >
        <ExternalLink />
        {t(`mailboxes.providers.${provider.type}.connect`)}
      </Button>
    </section>
  );
}

interface ImapValues {
  address: string;
  password: string;
  displayName: string;
  host: string;
  port: string;
  security: "tls" | "starttls";
  username: string;
  /** `false` accepts self-signed certificates (only if the admin allows it). */
  verifyCertificate: boolean;
}

const emptyValues: ImapValues = {
  address: "",
  password: "",
  displayName: "",
  host: "",
  port: "",
  security: "tls",
  username: "",
  verifyCertificate: true,
};

function connection(values: ImapValues): MailboxConnection {
  return {
    type: "imap",
    address: values.address.trim(),
    provider_settings: {
      host: values.host.trim(),
      security: values.security,
      ...(values.port && { port: Number(values.port) }),
      ...(values.username.trim() && { username: values.username.trim() }),
      ...(!values.verifyCertificate && { verify_certificate: false }),
    },
    credentials: { password: values.password },
  };
}

function ImapForm({
  onPreferOAuth,
  oauthTypes = [],
}: {
  onPreferOAuth: (type: MailboxType) => void;
  oauthTypes?: MailboxType[];
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const errorText = useMailErrorText();
  const [values, setValues] = useState(emptyValues);
  // Server fields the user edited are not overwritten by suggestions.
  const [serverEdited, setServerEdited] = useState(false);
  const [suggestion, setSuggestion] = useState<AutodiscoverSuggestion>();
  const [native, setNative] = useState<MailboxType>();
  const [tested, setTested] = useState<ConnectionTestResult>();
  const [submitError, setSubmitError] = useState<string>();
  const [showErrors, setShowErrors] = useState(false);

  const set = <K extends keyof ImapValues>(field: K, value: ImapValues[K]) => {
    setValues((current) => ({ ...current, [field]: value }));
    setTested(undefined);
    setSubmitError(undefined);
    if (field === "host" || field === "port" || field === "security") setServerEdited(true);
  };

  const discover = useMutation({
    mutationFn: (address: string) => autodiscover(address),
    meta: { errorToast: false },
    onSuccess: ({ suggestions }) => {
      const imap = suggestions.find((item) => item.type === "imap");
      setNative(
        suggestions.find((item) => item.type !== "imap" && oauthTypes.includes(item.type))?.type,
      );
      setSuggestion(imap);
      if (imap && !serverEdited) {
        const settings = imap.provider_settings as {
          host?: string;
          port?: number;
          security?: string;
        };
        setValues((current) => ({
          ...current,
          host: settings.host ?? current.host,
          port: settings.port ? String(settings.port) : current.port,
          security: settings.security === "starttls" ? "starttls" : "tls",
        }));
      }
    },
  });

  const errors = {
    address: ADDRESS.test(values.address.trim()) ? undefined : t("mailboxes.form.addressInvalid"),
    password: values.password ? undefined : t("mailboxes.form.passwordRequired"),
    host: values.host.trim() ? undefined : t("mailboxes.form.hostRequired"),
    port:
      !values.port || (Number(values.port) >= 1 && Number(values.port) <= 65535)
        ? undefined
        : t("mailboxes.form.portInvalid"),
  };
  const valid = !Object.values(errors).some(Boolean);

  const test = useMutation({
    mutationFn: () => testConnection(connection(values)),
    meta: { errorToast: false },
    onSuccess: setTested,
    onError: (error) => setSubmitError(errorText(problemErrorCode(error))),
  });
  const create = useMutation({
    mutationFn: () =>
      createMailbox({
        ...connection(values),
        sync_enabled: true,
        ...(values.displayName.trim() && { display_name: values.displayName.trim() }),
      }),
    meta: { errorToast: false },
    onSuccess: (mailbox) => {
      void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
      toast.success(t("mailboxes.created", { name: mailbox.display_name }));
      void navigate({ to: "/settings/mailboxes" });
    },
    onError: (error) => {
      const code = problemErrorCode(error);
      if (code) setSubmitError(errorText(code));
      else if (isApiError(error) && error.status === 409)
        setSubmitError(t("mailboxes.form.duplicate"));
      else setSubmitError(errorText(undefined));
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setShowErrors(true);
    if (valid) create.mutate();
  };
  const runTest = () => {
    setShowErrors(true);
    if (valid) test.mutate();
  };
  const busy = test.isPending || create.isPending;
  const error = (field: keyof typeof errors) => (showErrors ? errors[field] : undefined);

  return (
    <form
      onSubmit={submit}
      noValidate
      aria-labelledby="imap-form-title"
      className="flex flex-col gap-6"
    >
      <div>
        <h2 id="imap-form-title" className="text-sm font-medium">
          {t("mailboxes.form.title")}
        </h2>
        <p className="mt-1 text-ui text-muted-foreground">{t("mailboxes.form.description")}</p>
      </div>
      <div className="flex flex-col gap-4">
        <FormField
          label={t("mailboxes.form.address")}
          type="email"
          autoComplete="off"
          spellCheck={false}
          value={values.address}
          error={error("address")}
          onChange={(event) => set("address", event.target.value)}
          onBlur={() => {
            const address = values.address.trim();
            if (ADDRESS.test(address)) discover.mutate(address);
          }}
        />
        <FormField
          label={t("mailboxes.form.password")}
          type="password"
          autoComplete="new-password"
          value={values.password}
          error={error("password")}
          description={t("mailboxes.form.passwordDescription")}
          onChange={(event) => set("password", event.target.value)}
        />
        {suggestion && suggestion.hints.length > 0 && (
          <ul className="flex flex-col gap-1 rounded-md bg-muted px-3 py-2 text-ui text-muted-foreground">
            {suggestion.hints.map((hint) => (
              <li key={hint}>{t(`mailboxes.hints.${hint}`)}</li>
            ))}
          </ul>
        )}
        {native && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border px-3 py-2 text-ui">
            <span className="min-w-0 flex-1">{t(`mailboxes.providers.${native}.recommended`)}</span>
            <Button type="button" size="xs" variant="outline" onClick={() => onPreferOAuth(native)}>
              {t(`mailboxes.providers.${native}.title`)}
            </Button>
          </div>
        )}
        <FormField
          label={t("mailboxes.form.displayName")}
          placeholder={values.address || undefined}
          value={values.displayName}
          description={t("mailboxes.form.displayNameDescription")}
          onChange={(event) => set("displayName", event.target.value)}
        />
      </div>

      <fieldset className="flex flex-col gap-4 rounded-lg border px-4 pt-2 pb-4">
        <legend className="px-1 text-xs font-medium text-muted-foreground">
          {t("mailboxes.form.server")}
        </legend>
        {suggestion && (
          <p className="text-xs text-muted-foreground">
            {suggestion.source === "known"
              ? t("mailboxes.form.suggestedKnown")
              : t("mailboxes.form.suggestedGuess")}
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-[1fr_7rem]">
          <FormField
            label={t("mailboxes.form.host")}
            placeholder="imap.example.org"
            autoComplete="off"
            spellCheck={false}
            value={values.host}
            error={error("host")}
            onChange={(event) => set("host", event.target.value)}
          />
          <FormField
            label={t("mailboxes.form.port")}
            inputMode="numeric"
            placeholder={values.security === "tls" ? "993" : "143"}
            value={values.port}
            error={error("port")}
            onChange={(event) => set("port", event.target.value.replace(/\D/g, ""))}
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Label htmlFor="imap-security" className="text-ui">
              {t("mailboxes.form.security")}
            </Label>
            <NativeSelect
              id="imap-security"
              className="w-full"
              value={values.security}
              onChange={(event) =>
                set("security", event.target.value === "starttls" ? "starttls" : "tls")
              }
            >
              <NativeSelectOption value="tls">{t("mailboxes.form.securityTls")}</NativeSelectOption>
              <NativeSelectOption value="starttls">
                {t("mailboxes.form.securityStarttls")}
              </NativeSelectOption>
            </NativeSelect>
          </div>
          <FormField
            label={t("mailboxes.form.username")}
            placeholder={values.address || undefined}
            autoComplete="off"
            spellCheck={false}
            value={values.username}
            description={t("mailboxes.form.usernameDescription")}
            onChange={(event) => set("username", event.target.value)}
          />
        </div>
        <div className="flex items-start gap-3">
          <Checkbox
            id="imap-insecure"
            className="mt-0.5"
            checked={!values.verifyCertificate}
            onCheckedChange={(checked) => set("verifyCertificate", checked !== true)}
          />
          <Label htmlFor="imap-insecure" className="flex flex-col items-start gap-0.5 text-ui">
            <span>{t("mailboxes.form.acceptAnyCertificate")}</span>
            <span className="font-normal text-muted-foreground">
              {t("mailboxes.form.acceptAnyCertificateDescription")}
            </span>
          </Label>
        </div>
      </fieldset>

      <div aria-live="polite">
        {tested?.ok && (
          <p className="flex items-center gap-2 text-ui">
            <CircleCheck aria-hidden className="size-4 text-brand" />
            {t("mailboxes.form.testOk", { count: tested.folders?.length ?? 0 })}
          </p>
        )}
        {tested && !tested.ok && <FormError>{errorText(tested.error)}</FormError>}
        {submitError && <FormError>{submitError}</FormError>}
      </div>

      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={runTest}>
          {test.isPending ? t("mailboxes.form.testing") : t("mailboxes.form.test")}
        </Button>
        <Button type="submit" size="sm" disabled={busy}>
          {create.isPending ? t("mailboxes.form.adding") : t("mailboxes.add")}
        </Button>
      </div>
    </form>
  );
}
