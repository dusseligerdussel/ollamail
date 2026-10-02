import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Building2, FileKey, FolderTree, GitFork, Globe, KeyRound } from "lucide-react";
import { type FormEvent, type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminAuthQueryKey,
  createGitHubProvider,
  createLdapDirectory,
  createOidcProvider,
  createSamlProvider,
  type GitHubProvider,
  type GitHubProviderCreate,
  type LdapDirectory,
  type LdapDirectoryCreate,
  ldapPresets,
  type OidcPreset,
  type OidcProvider,
  type OidcProviderCreate,
  type SamlPreset,
  type SamlProvider,
  type SamlProviderCreate,
  updateGitHubProvider,
  updateLdapDirectory,
  updateOidcProvider,
  updateSamlProvider,
} from "@/api/admin-auth";
import { describeApiError, isApiError } from "@/api/errors";
import { Notice } from "@/components/admin/notice";
import { LdapTestPanel, OidcTestPanel } from "@/components/admin/provider-tests";
import { SamlEndpoints } from "@/components/admin/saml-endpoints";
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
import { cn } from "@/lib/utils";

/** What the admin picks in step 1; offered if the backend lists its kind. */
export type ProviderChoice = "entra" | "google" | "github" | "oidc" | "saml" | "ldap";

type Created =
  | { kind: "oidc"; provider: OidcProvider }
  | { kind: "github"; provider: GitHubProvider }
  | { kind: "saml"; provider: SamlProvider }
  | { kind: "ldap"; directory: LdapDirectory };

const choices: { id: ProviderChoice; icon: typeof KeyRound; kind: string }[] = [
  { id: "entra", icon: Building2, kind: "oidc" },
  { id: "google", icon: Globe, kind: "oidc" },
  { id: "github", icon: GitFork, kind: "github" },
  { id: "oidc", icon: KeyRound, kind: "oidc" },
  { id: "saml", icon: FileKey, kind: "saml" },
  { id: "ldap", icon: FolderTree, kind: "ldap" },
];

/** Lower-case key from the display name, e.g. "Microsoft Entra ID" → "microsoft-entra-id". */
export function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32)
    .replace(/-+$/, "");
}

export function AddProviderSheet({
  open,
  onOpenChange,
  kinds,
  initialChoice,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Provider types the backend can configure (`GET /admin/auth/settings`). */
  kinds: string[];
  /** Start at step 2 with this type (screenshots, deep links). */
  initialChoice?: ProviderChoice;
}) {
  const { t } = useTranslation();
  const [choice, setChoice] = useState<ProviderChoice | undefined>(initialChoice);
  const [created, setCreated] = useState<Created>();

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setChoice(undefined);
      setCreated(undefined);
    }
  }

  const step = created ? 3 : choice ? 2 : 1;
  return (
    <Sheet open={open} onOpenChange={close}>
      <SheetContent className="w-full gap-0 sm:max-w-lg" closeLabel={t("common.close")}>
        <SheetHeader className="border-b">
          <p className="text-xs text-muted-foreground">{t("pages.signIn.wizard.step", { step })}</p>
          <SheetTitle>
            {choice ? t(`pages.signIn.wizard.presets.${choice}`) : t("pages.signIn.wizard.title")}
          </SheetTitle>
          <SheetDescription className="text-ui">
            {step === 1 && t("pages.signIn.wizard.chooseType")}
            {step === 3 && created?.kind === "ldap" && t("pages.signIn.wizard.verifyLdap")}
            {step === 3 && created?.kind === "oidc" && t("pages.signIn.wizard.verifyOidc")}
            {step === 3 && created?.kind === "github" && t("pages.signIn.wizard.verifyGithub")}
            {step === 3 && created?.kind === "saml" && t("pages.signIn.wizard.verifySaml")}
          </SheetDescription>
        </SheetHeader>
        {step === 1 && <ChooseType kinds={kinds} onChoose={setChoice} />}
        {step === 2 && choice === "ldap" && (
          <LdapForm
            onBack={() => setChoice(undefined)}
            onCreated={(directory) => setCreated({ kind: "ldap", directory })}
          />
        )}
        {step === 2 && choice === "github" && (
          <GitHubForm
            onBack={() => setChoice(undefined)}
            onCreated={(provider) => setCreated({ kind: "github", provider })}
          />
        )}
        {step === 2 && choice === "saml" && (
          <SamlForm
            onBack={() => setChoice(undefined)}
            onCreated={(provider) => setCreated({ kind: "saml", provider })}
          />
        )}
        {step === 2 && choice && choice !== "ldap" && choice !== "github" && choice !== "saml" && (
          <OidcForm
            choice={choice}
            onBack={() => setChoice(undefined)}
            onCreated={(provider) => setCreated({ kind: "oidc", provider })}
          />
        )}
        {created && <Verify created={created} onDone={() => close(false)} />}
      </SheetContent>
    </Sheet>
  );
}

function ChooseType({
  kinds,
  onChoose,
}: {
  kinds: string[];
  onChoose: (choice: ProviderChoice) => void;
}) {
  const { t } = useTranslation();
  return (
    <ul className="flex flex-col gap-2 overflow-y-auto p-4">
      {choices.map(({ id, icon: Icon, kind }) => {
        const available = kinds.includes(kind);
        return (
          <li key={id}>
            <button
              type="button"
              disabled={!available}
              onClick={() => onChoose(id)}
              className="flex w-full items-center gap-3 rounded-lg border px-3.5 py-3 text-left outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
            >
              <Icon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block text-ui font-medium">
                  {t(`pages.signIn.wizard.presets.${id}`)}
                </span>
                <span className="block text-ui text-muted-foreground">
                  {available
                    ? t(`pages.signIn.wizard.presets.${id}Description`)
                    : t("pages.signIn.wizard.notAvailable")}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function FormBody({
  onSubmit,
  onBack,
  pending,
  error,
  children,
}: {
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onBack: () => void;
  pending: boolean;
  error: unknown;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <form onSubmit={onSubmit} className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-4 overflow-y-auto p-4">
        {children}
        {error ? <Notice tone="error">{describeError(error, t)}</Notice> : null}
      </div>
      <SheetFooter className="flex-row justify-between border-t">
        <Button type="button" variant="ghost" onClick={onBack}>
          {t("pages.signIn.wizard.back")}
        </Button>
        <Button type="submit" disabled={pending}>
          {pending ? t("pages.signIn.wizard.saving") : t("pages.signIn.wizard.next")}
        </Button>
      </SheetFooter>
    </form>
  );
}

function describeError(error: unknown, t: ReturnType<typeof useTranslation>["t"]) {
  const { title } = describeApiError(error, t);
  // Validation reasons from the backend are static English texts without user data.
  const detail = isApiError(error) && error.status === 422 ? error.problem?.detail : undefined;
  return detail ? `${title} (${detail})` : title;
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id} className="text-ui">
        {label}
      </Label>
      <NativeSelect
        id={id}
        className="w-full"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <NativeSelectOption key={option.value} value={option.value}>
            {option.label}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </div>
  );
}

const oidcDefaults: Record<Exclude<ProviderChoice, "ldap" | "github" | "saml">, string> = {
  entra: "Microsoft",
  google: "Google",
  oidc: "",
};

function OidcForm({
  choice,
  onBack,
  onCreated,
}: {
  choice: "entra" | "google" | "oidc";
  onBack: () => void;
  onCreated: (provider: OidcProvider) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState(oidcDefaults[choice]);
  const [name, setName] = useState(slugify(oidcDefaults[choice]));
  const [nameTouched, setNameTouched] = useState(false);
  const [preset, setPreset] = useState<OidcPreset>("keycloak");
  const [tenant, setTenant] = useState("");
  const [hostedDomain, setHostedDomain] = useState("");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  const create = useMutation({
    mutationFn: (body: OidcProviderCreate) => createOidcProvider(body),
    meta: { errorToast: false },
    onSuccess: async (provider) => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      onCreated(provider);
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const common = {
      name,
      display_name: displayName,
      client_id: clientId,
      client_secret: clientSecret || null,
      // Saved disabled; enabled in step 3 after the redirect URI is registered.
      enabled: false,
      // Backend defaults, spelled out because the generated type lists them as required.
      scopes: ["openid", "email", "profile"],
      auto_provision: true,
      link_by_email: false,
      allowed_domains: [],
      allowed_tenants: [],
      hosted_domains: [],
      groups_claim: "groups",
    };
    if (choice === "entra") {
      create.mutate({
        ...common,
        preset: "entra",
        issuer: `https://login.microsoftonline.com/${tenant.trim()}/v2.0`,
      });
    } else if (choice === "google") {
      create.mutate({
        ...common,
        preset: "google",
        issuer: "https://accounts.google.com",
        groups_claim: null,
        hosted_domains: hostedDomain.trim() ? [hostedDomain.trim()] : [],
      });
    } else {
      create.mutate({ ...common, preset, issuer: issuer.trim() });
    }
  }

  return (
    <FormBody onSubmit={onSubmit} onBack={onBack} pending={create.isPending} error={create.error}>
      <FormField
        label={t("pages.signIn.wizard.fields.displayName")}
        required
        value={displayName}
        onChange={(event) => {
          setDisplayName(event.target.value);
          if (!nameTouched) setName(slugify(event.target.value));
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.name")}
        description={t("pages.signIn.wizard.fields.nameHint")}
        required
        pattern="[a-z0-9]([a-z0-9\-]{0,30}[a-z0-9])?"
        value={name}
        onChange={(event) => {
          setNameTouched(true);
          setName(event.target.value);
        }}
      />
      {choice === "entra" && (
        <FormField
          label={t("pages.signIn.wizard.fields.tenant")}
          description={t("pages.signIn.wizard.fields.tenantHint")}
          required
          placeholder="00000000-0000-0000-0000-000000000000"
          value={tenant}
          onChange={(event) => setTenant(event.target.value)}
        />
      )}
      {choice === "google" && (
        <FormField
          label={t("pages.signIn.wizard.fields.hostedDomain")}
          description={t("pages.signIn.wizard.fields.hostedDomainHint")}
          placeholder="example.org"
          value={hostedDomain}
          onChange={(event) => setHostedDomain(event.target.value)}
        />
      )}
      {choice === "oidc" && (
        <>
          <SelectField
            label={t("pages.signIn.wizard.fields.preset")}
            value={preset}
            onChange={(value) => setPreset(value as OidcPreset)}
            options={(["keycloak", "authentik", "generic"] as const).map((value) => ({
              value,
              label: t(`pages.signIn.wizard.presetsOidc.${value}`),
            }))}
          />
          <FormField
            label={t("pages.signIn.wizard.fields.issuer")}
            type="url"
            required
            placeholder={
              preset === "keycloak"
                ? "https://sso.example.org/realms/staff"
                : "https://sso.example.org/application/o/ollamail/"
            }
            value={issuer}
            onChange={(event) => setIssuer(event.target.value)}
          />
        </>
      )}
      <FormField
        label={t("pages.signIn.wizard.fields.clientId")}
        required
        autoComplete="off"
        value={clientId}
        onChange={(event) => setClientId(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.clientSecret")}
        description={t("pages.signIn.wizard.fields.clientSecretHint")}
        type="password"
        autoComplete="new-password"
        value={clientSecret}
        onChange={(event) => setClientSecret(event.target.value)}
      />
    </FormBody>
  );
}

function GitHubForm({
  onBack,
  onCreated,
}: {
  onBack: () => void;
  onCreated: (provider: GitHubProvider) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("GitHub");
  const [name, setName] = useState("github");
  const [nameTouched, setNameTouched] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [organizations, setOrganizations] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  const create = useMutation({
    mutationFn: (body: GitHubProviderCreate) => createGitHubProvider(body),
    meta: { errorToast: false },
    onSuccess: async (provider) => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      onCreated(provider);
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate({
      name,
      display_name: displayName,
      base_url: baseUrl.trim() || null,
      client_id: clientId,
      client_secret: clientSecret,
      // Saved disabled; enabled in step 3 after the callback URL is registered.
      enabled: false,
      auto_provision: true,
      link_by_email: false,
      allowed_domains: [],
      allowed_organizations: organizations.split(/[\s,]+/).filter(Boolean),
      allowed_teams: [],
    });
  }

  return (
    <FormBody onSubmit={onSubmit} onBack={onBack} pending={create.isPending} error={create.error}>
      <FormField
        label={t("pages.signIn.wizard.fields.displayName")}
        required
        value={displayName}
        onChange={(event) => {
          setDisplayName(event.target.value);
          if (!nameTouched) setName(slugify(event.target.value));
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.name")}
        description={t("pages.signIn.wizard.fields.nameHint")}
        required
        pattern="[a-z0-9]([a-z0-9\-]{0,30}[a-z0-9])?"
        value={name}
        onChange={(event) => {
          setNameTouched(true);
          setName(event.target.value);
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.organizations")}
        description={t("pages.signIn.wizard.fields.organizationsHint")}
        placeholder="example-org"
        value={organizations}
        onChange={(event) => setOrganizations(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.baseUrl")}
        description={t("pages.signIn.wizard.fields.baseUrlHint")}
        type="url"
        placeholder="https://github.example.org"
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.clientId")}
        required
        autoComplete="off"
        value={clientId}
        onChange={(event) => setClientId(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.clientSecret")}
        type="password"
        autoComplete="new-password"
        required
        value={clientSecret}
        onChange={(event) => setClientSecret(event.target.value)}
      />
    </FormBody>
  );
}

const samlPresets: SamlPreset[] = ["entra", "adfs", "okta", "keycloak", "generic"];

const samlDefaultNames: Record<SamlPreset, string> = {
  entra: "Microsoft",
  adfs: "AD FS",
  okta: "Okta",
  keycloak: "Keycloak",
  generic: "",
};

function SamlForm({
  onBack,
  onCreated,
}: {
  onBack: () => void;
  onCreated: (provider: SamlProvider) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [preset, setPreset] = useState<SamlPreset>("entra");
  const [displayName, setDisplayName] = useState(samlDefaultNames.entra);
  const [displayNameTouched, setDisplayNameTouched] = useState(false);
  const [name, setName] = useState(slugify(samlDefaultNames.entra));
  const [nameTouched, setNameTouched] = useState(false);
  const [source, setSource] = useState<"url" | "file">("url");
  const [metadataUrl, setMetadataUrl] = useState("");
  const [metadataXml, setMetadataXml] = useState<string>();
  const [fileMissing, setFileMissing] = useState(false);

  const create = useMutation({
    mutationFn: (body: SamlProviderCreate) => createSamlProvider(body),
    meta: { errorToast: false },
    onSuccess: async (provider) => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      onCreated(provider);
    },
  });

  function choosePreset(value: SamlPreset) {
    setPreset(value);
    if (!displayNameTouched) {
      setDisplayName(samlDefaultNames[value]);
      if (!nameTouched) setName(slugify(samlDefaultNames[value]));
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Checked here instead of `required`: the file is read asynchronously.
    if (source === "file" && !metadataXml) {
      setFileMissing(true);
      return;
    }
    // Attribute names and NameID format are left out: the backend fills in the preset's.
    create.mutate({
      name,
      display_name: displayName,
      preset,
      ...(source === "url" ? { metadata_url: metadataUrl.trim() } : { metadata_xml: metadataXml }),
      // Saved disabled; enabled in step 3 after ollamail is registered at the IdP.
      enabled: false,
      // Backend defaults, spelled out because the generated type lists them as required.
      idp_certificates: [],
      trust_email: false,
      auto_provision: true,
      link_by_email: false,
      allowed_domains: [],
    });
  }

  return (
    <FormBody onSubmit={onSubmit} onBack={onBack} pending={create.isPending} error={create.error}>
      <SelectField
        label={t("pages.signIn.wizard.fields.preset")}
        value={preset}
        onChange={(value) => choosePreset(value as SamlPreset)}
        options={samlPresets.map((value) => ({
          value,
          label: t(`pages.signIn.wizard.presetsSaml.${value}`),
        }))}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.displayName")}
        required
        value={displayName}
        onChange={(event) => {
          setDisplayNameTouched(true);
          setDisplayName(event.target.value);
          if (!nameTouched) setName(slugify(event.target.value));
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.name")}
        description={t("pages.signIn.wizard.fields.samlNameHint")}
        required
        pattern="[a-z0-9]([a-z0-9\-]{0,30}[a-z0-9])?"
        value={name}
        onChange={(event) => {
          setNameTouched(true);
          setName(event.target.value);
        }}
      />
      <SelectField
        label={t("pages.signIn.wizard.fields.metadataSource")}
        value={source}
        onChange={(value) => setSource(value as typeof source)}
        options={(["url", "file"] as const).map((value) => ({
          value,
          label: t(`pages.signIn.wizard.metadataSources.${value}`),
        }))}
      />
      {source === "url" ? (
        <FormField
          label={t("pages.signIn.wizard.fields.metadataUrl")}
          description={t(`pages.signIn.wizard.metadataUrlHints.${preset}`)}
          type="url"
          required
          placeholder={samlMetadataPlaceholders[preset]}
          value={metadataUrl}
          onChange={(event) => setMetadataUrl(event.target.value)}
        />
      ) : (
        <FormField
          label={t("pages.signIn.wizard.fields.metadataFile")}
          description={t("pages.signIn.wizard.fields.metadataFileHint")}
          type="file"
          error={fileMissing ? t("pages.signIn.wizard.fields.metadataFileMissing") : undefined}
          accept=".xml,application/xml,text/xml,application/samlmetadata+xml"
          onChange={async (event) => {
            const file = event.target.files?.[0];
            setMetadataXml(file ? await file.text() : undefined);
            setFileMissing(false);
          }}
        />
      )}
    </FormBody>
  );
}

const samlMetadataPlaceholders: Record<SamlPreset, string> = {
  entra:
    "https://login.microsoftonline.com/<tenant>/federationmetadata/2007-06/federationmetadata.xml?appid=<app>",
  adfs: "https://adfs.example.org/FederationMetadata/2007-06/FederationMetadata.xml",
  okta: "https://example.okta.com/app/<app>/sso/saml/metadata",
  keycloak: "https://sso.example.org/realms/staff/protocol/saml/descriptor",
  generic: "https://idp.example.org/saml/metadata",
};

function LdapForm({
  onBack,
  onCreated,
}: {
  onBack: () => void;
  onCreated: (directory: LdapDirectory) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [directoryType, setDirectoryType] = useState<"active_directory" | "openldap">(
    "active_directory",
  );
  const [displayName, setDisplayName] = useState("");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [serverUrl, setServerUrl] = useState("");
  const [tlsMode, setTlsMode] = useState<"ldaps" | "starttls">("ldaps");
  const [bindDn, setBindDn] = useState("");
  const [bindPassword, setBindPassword] = useState("");
  const [baseDn, setBaseDn] = useState("");

  const create = useMutation({
    mutationFn: (body: LdapDirectoryCreate) => createLdapDirectory(body),
    meta: { errorToast: false },
    onSuccess: async (directory) => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      onCreated(directory);
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate({
      name,
      display_name: displayName,
      enabled: false,
      bind_password: bindPassword,
      settings: {
        ...ldapPresets[directoryType],
        directory_type: directoryType,
        server_urls: [serverUrl.trim()],
        tls_mode: tlsMode,
        bind_dn: bindDn.trim(),
        user_base_dn: baseDn.trim(),
        nested_groups: true,
        connect_timeout: 5,
        operation_timeout: 10,
      },
    });
  }

  return (
    <FormBody onSubmit={onSubmit} onBack={onBack} pending={create.isPending} error={create.error}>
      <SelectField
        label={t("pages.signIn.wizard.fields.directoryType")}
        value={directoryType}
        onChange={(value) => setDirectoryType(value as typeof directoryType)}
        options={(["active_directory", "openldap"] as const).map((value) => ({
          value,
          label: t(`pages.signIn.wizard.directoryTypes.${value}`),
        }))}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.displayName")}
        required
        value={displayName}
        onChange={(event) => {
          setDisplayName(event.target.value);
          if (!nameTouched) setName(slugify(event.target.value));
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.name")}
        description={t("pages.signIn.wizard.fields.nameHint")}
        required
        pattern="[a-z0-9][a-z0-9\-]{0,31}"
        value={name}
        onChange={(event) => {
          setNameTouched(true);
          setName(event.target.value);
        }}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.serverUrl")}
        description={t("pages.signIn.wizard.fields.serverUrlHint")}
        required
        value={serverUrl}
        onChange={(event) => setServerUrl(event.target.value)}
      />
      <SelectField
        label={t("pages.signIn.wizard.fields.tlsMode")}
        value={tlsMode}
        onChange={(value) => setTlsMode(value as typeof tlsMode)}
        options={(["ldaps", "starttls"] as const).map((value) => ({
          value,
          label: t(`pages.signIn.wizard.tlsModes.${value}`),
        }))}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.bindDn")}
        required
        placeholder="CN=svc-ollamail,OU=Service,DC=example,DC=org"
        value={bindDn}
        onChange={(event) => setBindDn(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.bindPassword")}
        type="password"
        autoComplete="new-password"
        required
        value={bindPassword}
        onChange={(event) => setBindPassword(event.target.value)}
      />
      <FormField
        label={t("pages.signIn.wizard.fields.userBaseDn")}
        required
        placeholder="OU=Staff,DC=example,DC=org"
        value={baseDn}
        onChange={(event) => setBaseDn(event.target.value)}
      />
    </FormBody>
  );
}

function Verify({ created, onDone }: { created: Created; onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const activate = useMutation({
    mutationFn: async () => {
      if (created.kind === "oidc") {
        await updateOidcProvider(created.provider.name, { enabled: true });
      } else if (created.kind === "github") {
        await updateGitHubProvider(created.provider.name, { enabled: true });
      } else if (created.kind === "saml") {
        await updateSamlProvider(created.provider.name, { enabled: true });
      } else {
        await updateLdapDirectory(created.directory, { enabled: true });
      }
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.wizard.done"));
      onDone();
    },
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={cn("flex flex-col gap-5 overflow-y-auto p-4")}>
        <Notice>{t("pages.signIn.wizard.savedDisabled")}</Notice>
        {created.kind === "saml" && <SamlEndpoints provider={created.provider} />}
        {(created.kind === "oidc" || created.kind === "github") && (
          <CopyField
            label={t("pages.signIn.provider.redirectUri")}
            value={created.provider.redirect_uri}
            description={t("pages.signIn.provider.redirectUriHint")}
          />
        )}
        {created.kind === "oidc" && <OidcTestPanel name={created.provider.name} />}
        {created.kind === "github" && (
          <p className="text-xs text-muted-foreground">{t("pages.signIn.provider.githubCheck")}</p>
        )}
        {created.kind === "ldap" && <LdapTestPanel name={created.directory.name} />}
      </div>
      <SheetFooter className="flex-row justify-between border-t">
        <Button type="button" variant="ghost" onClick={onDone}>
          {t("pages.signIn.wizard.later")}
        </Button>
        <Button type="button" disabled={activate.isPending} onClick={() => activate.mutate()}>
          {t("pages.signIn.wizard.activate")}
        </Button>
      </SheetFooter>
    </div>
  );
}
