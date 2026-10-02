import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { adminAuthQueryKey, refreshSamlMetadata, type SamlProvider } from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { Notice } from "@/components/admin/notice";
import { CopyField } from "@/components/copy-field";
import { Button } from "@/components/ui/button";

/** What the IdP admin registers for ollamail: entity ID, ACS URL or the SP metadata URL. */
export function SamlEndpoints({ provider }: { provider: SamlProvider }) {
  const { t } = useTranslation();
  // By default the metadata URL is also the entity ID; one field is enough then.
  const sameAsEntityId = provider.sp_metadata_url === provider.effective_sp_entity_id;
  return (
    <div className="flex flex-col gap-4">
      <CopyField
        label={
          sameAsEntityId
            ? t("pages.signIn.provider.spEntityIdAndMetadataUrl")
            : t("pages.signIn.provider.spMetadataUrl")
        }
        value={provider.sp_metadata_url}
        description={t("pages.signIn.provider.spMetadataUrlHint")}
      />
      {!sameAsEntityId && (
        <CopyField
          label={t("pages.signIn.provider.spEntityId")}
          value={provider.effective_sp_entity_id}
        />
      )}
      <CopyField
        label={t("pages.signIn.provider.acsUrl")}
        value={provider.redirect_uri}
        description={t("pages.signIn.provider.acsUrlHint")}
      />
    </div>
  );
}

/** Reload the IdP metadata from its URL, e.g. after a certificate rollover at the IdP. */
export function SamlMetadataRefresh({ provider }: { provider: SamlProvider }) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => refreshSamlMetadata(provider.name),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.provider.metadataRefreshed"));
    },
  });
  const date = new Intl.DateTimeFormat(i18n.resolvedLanguage, { dateStyle: "medium" });
  const expiry = provider.idp_certificates
    .map((certificate) => certificate.not_valid_after)
    .sort()
    .at(-1);

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        {t("pages.signIn.provider.certificates", {
          count: provider.idp_certificates.length,
          date: expiry ? date.format(new Date(expiry)) : "–",
        })}
      </p>
      {provider.metadata_url && (
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            {refresh.isPending
              ? t("pages.signIn.provider.refreshingMetadata")
              : t("pages.signIn.provider.refreshMetadata")}
          </Button>
        </div>
      )}
      {refresh.error ? (
        <Notice tone="error">{describeApiError(refresh.error, t).title}</Notice>
      ) : null}
    </div>
  );
}
