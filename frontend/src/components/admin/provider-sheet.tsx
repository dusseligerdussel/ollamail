import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminAuthQueryKey,
  deleteLdapDirectory,
  deleteOidcProvider,
  isAdminLockout,
  type LdapDirectory,
  type OidcProvider,
  updateLdapDirectory,
  updateOidcProvider,
} from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { Notice } from "@/components/admin/notice";
import { LdapTestPanel, OidcTestPanel } from "@/components/admin/provider-tests";
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
  | { kind: "ldap"; key: string; directory: LdapDirectory };

export function providerLabel(item: ProviderItem) {
  return item.kind === "oidc" ? item.provider.display_name : item.directory.display_name;
}

export function providerEnabled(item: ProviderItem) {
  return item.kind === "oidc" ? item.provider.enabled : item.directory.enabled;
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

  const toggle = useMutation({
    mutationFn: async () => {
      if (item.kind === "oidc") await updateOidcProvider(item.provider.name, { enabled: !enabled });
      else await updateLdapDirectory(item.directory, { enabled: !enabled });
    },
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.changed"));
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      item.kind === "oidc"
        ? deleteOidcProvider(item.provider.name)
        : deleteLdapDirectory(item.directory.name),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.removed"));
      onClose();
    },
  });
  const failed = toggle.error ?? remove.error;

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
          {item.kind === "oidc" ? (
            <>
              <Detail label={t("pages.signIn.provider.issuer")} value={item.provider.issuer} />
              <Detail label={t("pages.signIn.provider.clientId")} value={item.provider.client_id} />
            </>
          ) : (
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
        {item.kind === "oidc" && (
          <CopyField
            label={t("pages.signIn.provider.redirectUri")}
            value={item.provider.redirect_uri}
            description={t("pages.signIn.provider.redirectUriHint")}
          />
        )}
        {item.kind === "oidc" ? (
          <OidcTestPanel name={item.provider.name} />
        ) : (
          <LdapTestPanel name={item.directory.name} />
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
