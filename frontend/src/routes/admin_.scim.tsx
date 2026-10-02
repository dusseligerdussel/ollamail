import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  githubProvidersQueryOptions,
  ldapDirectoriesQueryOptions,
  oidcProvidersQueryOptions,
} from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import {
  createScimToken,
  revokeScimToken,
  type ScimSettings,
  type ScimToken,
  type ScimTokenIssued,
  scimQueryKey,
  scimSettingsQueryOptions,
  updateScimSettings,
} from "@/api/scim";
import { AdminSection, AdminSubPage } from "@/components/admin/admin-page";
import { ConfirmDialog } from "@/components/admin/confirm-dialog";
import { Notice } from "@/components/admin/notice";
import { CopyField } from "@/components/copy-field";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin_/scim")({
  component: ScimPage,
});

interface ProviderOption {
  key: string;
  label: string;
}

/** Validity choices for new tokens, in days (`null`: no expiry). */
const expiryChoices = [null, 90, 365] as const;

function ScimPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  if (!isAdmin) return <Forbidden />;
  return (
    <AdminSubPage title={t("pages.scim.title")} backLabel={t("pages.scim.back")}>
      <ScimContent />
    </AdminSubPage>
  );
}

function ScimContent() {
  const { t } = useTranslation();
  const [scim, oidc, github, ldap] = useQueries({
    queries: [
      scimSettingsQueryOptions,
      oidcProvidersQueryOptions,
      githubProvidersQueryOptions,
      ldapDirectoriesQueryOptions,
    ],
  });
  if (scim.isPending || oidc.isPending || github.isPending || ldap.isPending) {
    return <ListSkeleton />;
  }
  const error = scim.error ?? oidc.error ?? github.error ?? ldap.error;
  if (error || !scim.data) return <InlineError error={error} />;

  const providers: ProviderOption[] = [
    ...(oidc.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(github.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(ldap.data ?? []).map((d) => ({ key: d.provider, label: d.display_name })),
  ];
  return (
    <div className="flex flex-col gap-8">
      <GeneralSection settings={scim.data} />
      <TokensSection tokens={scim.data.tokens} />
      <LinkingSection
        key={scim.data.link_providers.join("\n")}
        saved={scim.data.link_providers}
        providers={providers}
      />
      <Notice>{t("pages.scim.groupsHint")}</Notice>
    </div>
  );
}

function useSaveSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateScimSettings,
    onSuccess: (data) => {
      queryClient.setQueryData(scimQueryKey, data);
      toast.success(t("pages.scim.saved"));
    },
  });
}

function GeneralSection({ settings }: { settings: ScimSettings }) {
  const { t } = useTranslation();
  const id = useId();
  const save = useSaveSettings();
  const { stats } = settings;
  return (
    <AdminSection id={`${id}-general`} title={t("pages.scim.general")}>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <div id={`${id}-enabled`} className="text-ui font-medium">
            {t("pages.scim.enabled")}
          </div>
          <div className="text-ui text-muted-foreground">{t("pages.scim.enabledDescription")}</div>
        </div>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          aria-labelledby={`${id}-enabled`}
          className="w-full shrink-0 sm:w-40"
          value={settings.enabled ? "on" : "off"}
          disabled={save.isPending}
          onValueChange={(value) => value && save.mutate({ enabled: value === "on" })}
        >
          <ToggleGroupItem value="off" className="flex-1">
            {t("pages.scim.off")}
          </ToggleGroupItem>
          <ToggleGroupItem value="on" className="flex-1">
            {t("pages.scim.on")}
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="px-4 py-3.5">
        <CopyField
          label={t("pages.scim.endpoint")}
          value={settings.endpoint_url}
          description={t("pages.scim.endpointDescription")}
        />
      </div>
      <p className="px-4 py-3.5 text-ui text-muted-foreground">
        {stats.users === 0 && stats.groups === 0
          ? t("pages.scim.statsEmpty")
          : t("pages.scim.stats", {
              users: stats.users,
              active: stats.active_users,
              groups: stats.groups,
            })}
      </p>
    </AdminSection>
  );
}

function useDateFormat() {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const format = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
    dateStyle: "medium",
    timeZone: timezone,
  });
  return (value: string) => format.format(new Date(value));
}

function TokensSection({ tokens }: { tokens: ScimToken[] }) {
  const { t } = useTranslation();
  const id = useId();
  const [issued, setIssued] = useState<ScimTokenIssued>();
  const [revoking, setRevoking] = useState<ScimToken>();
  return (
    <AdminSection
      id={`${id}-tokens`}
      title={t("pages.scim.tokens")}
      description={t("pages.scim.tokensDescription")}
    >
      {issued && <IssuedToken issued={issued} onDone={() => setIssued(undefined)} />}
      {tokens.length === 0 ? (
        <p className="px-4 py-3.5 text-ui text-muted-foreground">{t("pages.scim.noTokens")}</p>
      ) : (
        <ul aria-label={t("pages.scim.tokens")} className="divide-y">
          {tokens.map((token) => (
            <TokenRow key={token.id} token={token} onRevoke={() => setRevoking(token)} />
          ))}
        </ul>
      )}
      <CreateTokenForm onCreated={setIssued} />
      <RevokeDialog token={revoking} onClose={() => setRevoking(undefined)} />
    </AdminSection>
  );
}

function TokenRow({ token, onRevoke }: { token: ScimToken; onRevoke: () => void }) {
  const { t } = useTranslation();
  const date = useDateFormat();
  const expired = token.expires_at !== null && new Date(token.expires_at) <= new Date();
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <KeyRound aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="truncate text-ui font-medium">{token.name}</span>
          <code className="font-mono text-xs text-muted-foreground">{token.hint}…</code>
          {expired && <Badge variant="outline">{t("pages.scim.expired")}</Badge>}
        </div>
        <div className="text-xs text-muted-foreground">
          {t("pages.scim.created", { date: date(token.created_at) })}
          {" · "}
          {token.last_used_at
            ? t("pages.scim.lastUsed", { date: date(token.last_used_at) })
            : t("pages.scim.neverUsed")}
          {token.expires_at && !expired && (
            <>
              {" · "}
              {t("pages.scim.expires", { date: date(token.expires_at) })}
            </>
          )}
        </div>
      </div>
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        aria-label={t("pages.scim.revokeLabel", { name: token.name })}
        onClick={onRevoke}
      >
        <Trash2 aria-hidden />
      </Button>
    </li>
  );
}

function IssuedToken({ issued, onDone }: { issued: ScimTokenIssued; onDone: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 px-4 py-3.5">
      <Notice tone="warning">{t("pages.scim.copyNow")}</Notice>
      <CopyField
        label={t("pages.scim.secret", { name: issued.token.name })}
        value={issued.secret}
      />
      <div>
        <Button type="button" size="sm" variant="outline" onClick={onDone}>
          {t("pages.scim.done")}
        </Button>
      </div>
    </div>
  );
}

function CreateTokenForm({ onCreated }: { onCreated: (issued: ScimTokenIssued) => void }) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("");
  const create = useMutation({
    mutationFn: () => createScimToken(name.trim(), expiry ? Number(expiry) : null),
    meta: { errorToast: false },
    onSuccess: async (issued) => {
      setName("");
      onCreated(issued);
      await queryClient.invalidateQueries({ queryKey: scimQueryKey });
    },
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (name.trim()) create.mutate();
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3 px-4 py-3.5">
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem_auto] sm:items-end">
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${id}-name`} className="text-ui">
            {t("pages.scim.tokenName")}
          </Label>
          <Input
            id={`${id}-name`}
            className="h-8"
            maxLength={100}
            placeholder={t("pages.scim.tokenNamePlaceholder")}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${id}-expiry`} className="text-ui">
            {t("pages.scim.validity")}
          </Label>
          <NativeSelect
            id={`${id}-expiry`}
            size="sm"
            className="w-full"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
          >
            {expiryChoices.map((days) => (
              <NativeSelectOption key={days ?? "none"} value={days ?? ""}>
                {days === null ? t("pages.scim.noExpiry") : t("pages.scim.days", { count: days })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <Button type="submit" size="sm" disabled={!name.trim() || create.isPending}>
          <Plus aria-hidden />
          {t("pages.scim.createToken")}
        </Button>
      </div>
      {create.isError && <Notice tone="error">{describeApiError(create.error, t).title}</Notice>}
    </form>
  );
}

function RevokeDialog({ token, onClose }: { token?: ScimToken; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const revoke = useMutation({
    mutationFn: (tokenId: string) => revokeScimToken(tokenId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: scimQueryKey });
      toast.success(t("pages.scim.revoked"));
      onClose();
    },
  });
  return (
    <ConfirmDialog
      open={token !== undefined}
      onOpenChange={(open) => {
        if (!open) {
          revoke.reset();
          onClose();
        }
      }}
      title={t("pages.scim.revokeTitle", { name: token?.name ?? "" })}
      description={t("pages.scim.revokeDescription")}
      confirmLabel={t("pages.scim.revoke")}
      cancelLabel={t("pages.scim.cancel")}
      pending={revoke.isPending}
      onConfirm={() => token && revoke.mutate(token.id)}
    />
  );
}

function LinkingSection({ saved, providers }: { saved: string[]; providers: ProviderOption[] }) {
  const { t } = useTranslation();
  const id = useId();
  const save = useSaveSettings();
  const [selected, setSelected] = useState(() => new Set(saved));
  const changed =
    selected.size !== saved.length || saved.some((provider) => !selected.has(provider));

  function toggle(key: string, checked: boolean) {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  return (
    <AdminSection
      id={`${id}-linking`}
      title={t("pages.scim.linking")}
      description={t("pages.scim.linkingDescription")}
    >
      {providers.length === 0 ? (
        <p className="px-4 py-3.5 text-ui text-muted-foreground">{t("pages.scim.noProviders")}</p>
      ) : (
        <ul aria-label={t("pages.scim.linking")} className="divide-y">
          {providers.map((provider) => {
            const checkboxId = `${id}-${provider.key}`;
            return (
              <li key={provider.key} className="flex items-center gap-3 px-4 py-2.5">
                <Checkbox
                  id={checkboxId}
                  checked={selected.has(provider.key)}
                  onCheckedChange={(checked) => toggle(provider.key, checked === true)}
                />
                <Label htmlFor={checkboxId} className="min-w-0 flex-1 text-ui font-normal">
                  {provider.label}
                </Label>
              </li>
            );
          })}
        </ul>
      )}
      {providers.length > 0 && (
        <div className="flex justify-end px-4 py-2.5">
          <Button
            type="button"
            size="sm"
            disabled={!changed || save.isPending}
            onClick={() => save.mutate({ link_providers: [...selected].sort() })}
          >
            {t("pages.scim.saveLinking")}
          </Button>
        </div>
      )}
    </AdminSection>
  );
}
