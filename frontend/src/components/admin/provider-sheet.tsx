import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminAuthQueryKey,
  deleteGitHubProvider,
  deleteLdapDirectory,
  deleteOidcProvider,
  deleteSamlProvider,
  type GitHubProvider,
  isAdminLockout,
  type LdapDirectory,
  type OidcProvider,
  type SamlProvider,
  updateGitHubProvider,
  updateLdapDirectory,
  updateOidcProvider,
  updateSamlProvider,
} from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { isReauthCancelled } from "@/api/reauth";
import { Notice } from "@/components/admin/notice";
import { LdapTestPanel, OidcTestPanel } from "@/components/admin/provider-tests";
import { SamlEndpoints, SamlMetadataRefresh } from "@/components/admin/saml-endpoints";
import { useReauth } from "@/components/auth/reauth";
import { CopyField } from "@/components/copy-field";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

export type ProviderItem =
  | { kind: "oidc"; key: string; provider: OidcProvider }
  | { kind: "github"; key: string; provider: GitHubProvider }
  | { kind: "saml"; key: string; provider: SamlProvider }
  | { kind: "ldap"; key: string; directory: LdapDirectory };

export function providerLabel(item: ProviderItem) {
  return item.kind === "ldap" ? item.directory.display_name : item.provider.display_name;
}

export function providerEnabled(item: ProviderItem) {
  return item.kind === "ldap" ? item.directory.enabled : item.provider.enabled;
}

/** Name in the provider's own admin API (`/admin/auth/oidc/providers/{name}` etc.). */
function apiName(item: ProviderItem) {
  return item.kind === "ldap" ? item.directory.name : item.provider.name;
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="break-all text-ui">{value}</dd>
    </div>
  );
}

/** Details of one provider: redirect URI, connection test, enable/disable, remove. */
export function ProviderSheet({
  item,
  typeLabel,
  onOpenChange,
}: {
  item: ProviderItem | undefined;
  typeLabel: string;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <Sheet open={item !== undefined} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 sm:max-w-lg" closeLabel={t("common.close")}>
        {item && (
          <ProviderDetails item={item} typeLabel={typeLabel} onClose={() => onOpenChange(false)} />
        )}
      </SheetContent>
    </Sheet>
  );
}

function ProviderDetails({
  item,
  typeLabel,
  onClose,
}: {
  item: ProviderItem;
  typeLabel: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const enabled = providerEnabled(item);
  const readOnly = item.kind === "oidc" && item.provider.source === "env";

  const withReauth = useReauth();
  const toggle = useMutation({
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: () =>
      withReauth(async () => {
        const change = { enabled: !enabled };
        if (item.kind === "oidc") await updateOidcProvider(item.provider.name, change);
        else if (item.kind === "github") await updateGitHubProvider(item.provider.name, change);
        else if (item.kind === "saml") await updateSamlProvider(item.provider.name, change);
        else await updateLdapDirectory(item.directory, change);
      }),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.changed"));
    },
  });
  const remove = useMutation({
    mutationFn: () => {
      if (item.kind === "oidc") return deleteOidcProvider(apiName(item));
      if (item.kind === "github") return deleteGitHubProvider(apiName(item));
      if (item.kind === "saml") return deleteSamlProvider(apiName(item));
      return deleteLdapDirectory(apiName(item));
    },
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.removed"));
      onClose();
    },
  });
  const failed = isReauthCancelled(toggle.error) ? remove.error : (toggle.error ?? remove.error);

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle>{providerLabel(item)}</SheetTitle>
        <SheetDescription className="text-ui">
          {typeLabel} · {enabled ? t("pages.signIn.enabled") : t("pages.signIn.disabled")}
        </SheetDescription>
      </SheetHeader>
      <div className="flex flex-col gap-5 overflow-y-auto p-4">
        {readOnly && <Notice>{t("pages.signIn.provider.readOnly")}</Notice>}
        <dl className="grid gap-3">
          <Detail label={t("pages.signIn.provider.key")} value={item.key} />
          {item.kind === "oidc" && (
            <>
              <Detail label={t("pages.signIn.provider.issuer")} value={item.provider.issuer} />
              <Detail label={t("pages.signIn.provider.clientId")} value={item.provider.client_id} />
            </>
          )}
          {item.kind === "github" && (
            <>
              <Detail
                label={t("pages.signIn.provider.server")}
                value={item.provider.base_url ?? "https://github.com"}
              />
              <Detail label={t("pages.signIn.provider.clientId")} value={item.provider.client_id} />
              {item.provider.allowed_organizations.length > 0 && (
                <Detail
                  label={t("pages.signIn.provider.organizations")}
                  value={item.provider.allowed_organizations.join(", ")}
                />
              )}
            </>
          )}
          {item.kind === "saml" && (
            <>
              <Detail
                label={t("pages.signIn.provider.idpEntityId")}
                value={item.provider.idp_entity_id}
              />
              <Detail label={t("pages.signIn.provider.ssoUrl")} value={item.provider.idp_sso_url} />
              {item.provider.metadata_url && (
                <Detail
                  label={t("pages.signIn.provider.metadataUrl")}
                  value={item.provider.metadata_url}
                />
              )}
            </>
          )}
          {item.kind === "ldap" && (
            <>
              <Detail
                label={t("pages.signIn.provider.servers")}
                value={item.directory.settings.server_urls.join(", ")}
              />
              <Detail
                label={t("pages.signIn.provider.baseDn")}
                value={item.directory.settings.user_base_dn}
              />
            </>
          )}
        </dl>
        {item.kind === "saml" && (
          <>
            <SamlMetadataRefresh provider={item.provider} />
            <SamlEndpoints provider={item.provider} />
          </>
        )}
        {(item.kind === "oidc" || item.kind === "github") && (
          <CopyField
            label={t("pages.signIn.provider.redirectUri")}
            value={item.provider.redirect_uri}
            description={t("pages.signIn.provider.redirectUriHint")}
          />
        )}
        {item.kind === "oidc" && <OidcTestPanel name={item.provider.name} />}
        {item.kind === "ldap" && <LdapTestPanel name={item.directory.name} />}
        {item.kind === "github" && (
          <p className="text-xs text-muted-foreground">{t("pages.signIn.provider.githubCheck")}</p>
        )}
        {failed ? (
          <Notice tone="error">
            {isAdminLockout(failed) ? t("pages.signIn.lockout") : describeApiError(failed, t).title}
          </Notice>
        ) : null}
        {confirmRemove && <Notice tone="warning">{t("pages.signIn.provider.removeHint")}</Notice>}
      </div>
      {!readOnly && (
        <SheetFooter className="flex-row justify-between border-t">
          <Button
            type="button"
            variant={confirmRemove ? "destructive" : "ghost"}
            disabled={remove.isPending}
            onClick={() => (confirmRemove ? remove.mutate() : setConfirmRemove(true))}
          >
            {confirmRemove
              ? t("pages.signIn.provider.removeConfirm")
              : t("pages.signIn.provider.remove")}
          </Button>
          <Button
            type="button"
            variant={enabled ? "outline" : "default"}
            disabled={toggle.isPending}
            onClick={() => toggle.mutate()}
          >
            {enabled ? t("pages.signIn.disable") : t("pages.signIn.enable")}
          </Button>
        </SheetFooter>
      )}
    </>
  );
}
