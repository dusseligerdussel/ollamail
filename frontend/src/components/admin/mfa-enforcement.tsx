import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { adminAuthQueryKey, updateAuthSettings } from "@/api/admin-auth";
import type { components } from "@/api/schema.gen";
import { AdminSection } from "@/components/admin/admin-page";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

type MfaEnforcement = components["schemas"]["MfaEnforcement"];

const OPTIONS: MfaEnforcement[] = ["off", "admins", "all"];

/** Which local accounts must use a second factor (Admin → Sign-in). */
export function MfaEnforcementSection({ value }: { value: MfaEnforcement }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const change = useMutation({
    mutationFn: (next: MfaEnforcement) => updateAuthSettings({ mfa_enforcement: next }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.signIn.mfa.changed"));
    },
  });
  const current = (change.isPending && change.variables) || value;

  return (
    <AdminSection id="sign-in-mfa" title={t("pages.signIn.mfa.title")} className="mt-8">
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="min-w-0">
          <label htmlFor="sign-in-mfa-enforcement" className="text-ui font-medium">
            {t("pages.signIn.mfa.label")}
          </label>
          <p className="text-ui text-muted-foreground">{t("pages.signIn.mfa.description")}</p>
          {current === "off" && (
            <p className="mt-1 text-ui text-muted-foreground">
              {t("pages.signIn.mfa.recommended")}
            </p>
          )}
        </div>
        <div className="w-full shrink-0 sm:w-60">
          <NativeSelect
            id="sign-in-mfa-enforcement"
            size="sm"
            className="w-full"
            value={current}
            disabled={change.isPending}
            onChange={(event) => change.mutate(event.target.value as MfaEnforcement)}
          >
            {OPTIONS.map((option) => (
              <NativeSelectOption key={option} value={option}>
                {t(`pages.signIn.mfa.options.${option}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      </div>
    </AdminSection>
  );
}
