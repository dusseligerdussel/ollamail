import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import {
  ChevronRight,
  FileKey,
  FolderTree,
  GitFork,
  KeyRound,
  Plus,
  UserRound,
} from "lucide-react";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminAuthQueryKey,
  authSettingsQueryOptions,
  githubProvidersQueryOptions,
  isAdminLockout,
  ldapDirectoriesQueryOptions,
  oidcProvidersQueryOptions,
  samlProvidersQueryOptions,
  updateAuthSettings,
} from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { AddProviderSheet } from "@/components/admin/add-provider-sheet";
import { AdminSection, AdminSubPage } from "@/components/admin/admin-page";
import { ConfirmDialog } from "@/components/admin/confirm-dialog";
import { MfaEnforcementSection } from "@/components/admin/mfa-enforcement";
import { Notice } from "@/components/admin/notice";
import {
  type ProviderItem,
  ProviderSheet,
  providerEnabled,
  providerLabel,
} from "@/components/admin/provider-sheet";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { retryFailed } from "@/lib/retry-failed";

export const Route = createFileRoute("/admin_/sign-in")({
  component: SignInAdminPage,
});

function SignInAdminPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const [adding, setAdding] = useState(false);
  if (!isAdmin) return <Forbidden />;

  return (
    <AdminSubPage
      title={t("pages.signIn.title")}
      backLabel={t("pages.signIn.back")}
      actions={
        <Button size="sm" onClick={() => setAdding(true)}>
          <Plus aria-hidden />
          {t("pages.signIn.addProvider")}
        </Button>
      }
    >
      <SignInMethods adding={adding} onAddingChange={setAdding} />
    </AdminSubPage>
  );
}

function useTypeLabel() {
  const { t } = useTranslation();
  return (item: ProviderItem) => {
    if (item.kind === "ldap") {
      return t(`pages.signIn.wizard.directoryTypes.${item.directory.settings.directory_type}`);
    }
    if (item.kind === "saml") {
      return `${t("pages.signIn.kinds.saml")} · ${t(`pages.signIn.wizard.presetsSaml.${item.provider.preset}`)}`;
    }
    if (item.kind === "github") {
      return item.provider.base_url
        ? t("pages.signIn.wizard.presets.githubEnterprise")
        : t("pages.signIn.wizard.presets.github");
    }
    const preset = item.provider.preset;
    if (preset === "entra" || preset === "google")
      return t(`pages.signIn.wizard.presets.${preset}`);
    return t(`pages.signIn.wizard.presetsOidc.${preset}`);
  };
}

function SignInMethods({
  adding,
  onAddingChange,
}: {
  adding: boolean;
  onAddingChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const typeLabel = useTypeLabel();
  const [settings, oidc, github, ldap, saml] = useQueries({
    queries: [
      authSettingsQueryOptions,
      oidcProvidersQueryOptions,
      githubProvidersQueryOptions,
      ldapDirectoriesQueryOptions,
      samlProvidersQueryOptions,
    ],
  });
  const [selected, setSelected] = useState<string>();

  if (
    settings.isPending ||
    oidc.isPending ||
    github.isPending ||
    ldap.isPending ||
    saml.isPending
  ) {
    return <ListSkeleton />;
  }
  const error = settings.error ?? oidc.error ?? github.error ?? ldap.error ?? saml.error;
  if (error || !settings.data || !oidc.data || !github.data || !ldap.data || !saml.data) {
    return <InlineError error={error} {...retryFailed(settings, oidc, github, ldap, saml)} />;
  }

  const items: ProviderItem[] = [
    ...oidc.data.map((provider) => ({
      kind: "oidc" as const,
      key: provider.provider,
      provider,
    })),
    ...github.data.map((provider) => ({
      kind: "github" as const,
      key: provider.provider,
      provider,
    })),
    ...saml.data.map((provider) => ({
      kind: "saml" as const,
      key: provider.provider,
      provider,
    })),
    ...ldap.data.map((directory) => ({
      kind: "ldap" as const,
      key: directory.provider,
      directory,
    })),
  ];
  const current = items.find((item) => item.key === selected);
  const access = settings.data.admin_access;

  return (
    <>
      <AdminSection id="sign-in-methods" title={t("pages.signIn.methods")}>
        <LocalLoginRow
          enabled={settings.data.local_login_enabled}
          ownProviders={access.own_providers}
        />
        {items.map((item) => {
          const Icon = { oidc: KeyRound, github: GitFork, saml: FileKey, ldap: FolderTree }[
            item.kind
          ];
          const enabled = providerEnabled(item);
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setSelected(item.key)}
              className="flex w-full items-center gap-3 px-4 py-3.5 text-left outline-none first:rounded-t-lg last:rounded-b-lg hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80"
            >
              <Icon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-ui font-medium">{providerLabel(item)}</span>
                <span className="block truncate text-ui text-muted-foreground">
                  {typeLabel(item)} · {item.key}
                </span>
              </span>
              {item.kind === "oidc" && item.provider.source === "env" && (
                <Badge variant="outline" className="hidden font-normal sm:inline-flex">
                  {t("pages.signIn.fromEnvironment")}
                </Badge>
              )}
              <Badge variant={enabled ? "secondary" : "outline"} className="font-normal">
                {enabled ? t("pages.signIn.enabled") : t("pages.signIn.disabled")}
              </Badge>
              <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />
            </button>
          );
        })}
      </AdminSection>
      {items.length === 0 && (
        <Notice className="mt-3">
          <span>{t("pages.signIn.emptyProviders")}</span>{" "}
          <Button
            variant="link"
            className="h-auto p-0 text-ui"
            onClick={() => onAddingChange(true)}
          >
            {t("pages.signIn.addProvider")}
          </Button>
        </Notice>
      )}
      <MfaEnforcementSection value={settings.data.mfa_enforcement} />
      <div className="mt-6 flex flex-col gap-1.5 text-ui text-muted-foreground">
        <p>{t("pages.signIn.adminAccess", { count: access.usable_admins })}</p>
        <p>
          <Trans
            i18nKey="pages.signIn.emergency"
            components={{ code: <code className="font-mono text-xs" /> }}
          />
        </p>
      </div>
      <ProviderSheet
        item={current}
        typeLabel={current ? typeLabel(current) : ""}
        onOpenChange={(open) => !open && setSelected(undefined)}
      />
      <AddProviderSheet
        open={adding}
        onOpenChange={onAddingChange}
        kinds={settings.data.provider_kinds}
      />
    </>
  );
}

function LocalLoginRow({ enabled, ownProviders }: { enabled: boolean; ownProviders: string[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const change = useMutation({
    mutationFn: (value: boolean) => updateAuthSettings({ local_login_enabled: value }),
    meta: { errorToast: false },
    onSuccess: async () => {
      setConfirming(false);
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.changed"));
    },
  });
  // Without a working external sign-in the admin would lock themselves out (the server only
  // refuses when *no* admin could sign in any more).
  const onlyLocal = !ownProviders.some((provider) => provider !== "local");

  let error: string | undefined;
  if (change.isError) {
    error = isAdminLockout(change.error)
      ? t("pages.signIn.disableLocal.lockout")
      : describeApiError(change.error, t).title;
  }

  return (
    <div className="flex items-center gap-3 px-4 py-3.5">
      <UserRound aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-ui font-medium">{t("pages.signIn.local")}</div>
        <div className="text-ui text-muted-foreground">{t("pages.signIn.localDescription")}</div>
      </div>
      <Badge
        variant={enabled ? "secondary" : "outline"}
        className="hidden font-normal sm:inline-flex"
      >
        {enabled ? t("pages.signIn.enabled") : t("pages.signIn.disabled")}
      </Badge>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0 text-ui"
        disabled={change.isPending}
        onClick={() => {
          change.reset();
          if (enabled) setConfirming(true);
          else change.mutate(true);
        }}
      >
        {enabled ? t("pages.signIn.disable") : t("pages.signIn.enable")}
      </Button>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("pages.signIn.disableLocal.title")}
        description={t("pages.signIn.disableLocal.description")}
        warning={onlyLocal ? t("pages.signIn.disableLocal.selfWarning") : undefined}
        error={error}
        confirmLabel={t("pages.signIn.disableLocal.confirm")}
        cancelLabel={t("pages.signIn.disableLocal.cancel")}
        pending={change.isPending}
        onConfirm={() => change.mutate(false)}
      />
    </div>
  );
}
