import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Plus, Trash2 } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminAuthQueryKey,
  githubProvidersQueryOptions,
  ldapDirectoriesQueryOptions,
  oidcProvidersQueryOptions,
  type Role,
  type RoleMapping,
  roleMappingQueryOptions,
  roles,
  saveRoleMapping,
  testRoleMapping,
} from "@/api/admin-auth";
import { describeApiError, isApiError } from "@/api/errors";
import { SCIM_PROVIDER } from "@/api/scim";
import { AdminSection, AdminSubPage } from "@/components/admin/admin-page";
import { Notice } from "@/components/admin/notice";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin_/role-mapping")({
  component: RoleMappingPage,
});

interface ProviderOption {
  key: string;
  label: string;
}

function RoleMappingPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  if (!isAdmin) return <Forbidden />;
  return (
    <AdminSubPage title={t("pages.roleMapping.title")} backLabel={t("pages.roleMapping.back")}>
      <RoleMappingContent />
    </AdminSubPage>
  );
}

function RoleMappingContent() {
  const { t } = useTranslation();
  const [mapping, oidc, github, ldap] = useQueries({
    queries: [
      roleMappingQueryOptions,
      oidcProvidersQueryOptions,
      githubProvidersQueryOptions,
      ldapDirectoriesQueryOptions,
    ],
  });
  if (mapping.isPending || oidc.isPending || github.isPending || ldap.isPending) {
    return <ListSkeleton />;
  }
  const error = mapping.error ?? oidc.error ?? github.error ?? ldap.error;
  if (error || !mapping.data) return <InlineError error={error} />;

  const providers: ProviderOption[] = [
    ...(oidc.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(github.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(ldap.data ?? []).map((d) => ({ key: d.provider, label: d.display_name })),
    { key: SCIM_PROVIDER, label: t("pages.scim.providerLabel") },
  ];
  // Re-mount the form when the saved mapping changes (after saving).
  return (
    <>
      <MappingForm key={JSON.stringify(mapping.data)} saved={mapping.data} providers={providers} />
      <MappingTest providers={providers} />
    </>
  );
}

interface DraftRule {
  id: string;
  group: string;
  provider: string;
  role: Role;
}

let draftIds = 0;
const newId = () => `draft-${++draftIds}`;

function MappingForm({ saved, providers }: { saved: RoleMapping; providers: ProviderOption[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const id = useId();
  const [enabled, setEnabled] = useState(saved.enabled);
  const [defaultRole, setDefaultRole] = useState<Role>(saved.default_role);
  const [rules, setRules] = useState<DraftRule[]>(() =>
    saved.rules.map((rule) => ({
      id: rule.id,
      group: rule.group,
      provider: rule.provider ?? "",
      role: rule.role,
    })),
  );

  const save = useMutation({
    mutationFn: () =>
      saveRoleMapping({
        enabled,
        default_role: defaultRole,
        rules: rules
          .filter((rule) => rule.group.trim())
          .map((rule) => ({
            group: rule.group.trim(),
            provider: rule.provider || null,
            role: rule.role,
          })),
      }),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: adminAuthQueryKey });
      toast.success(t("pages.roleMapping.saved"));
    },
  });

  function update(ruleId: string, changes: Partial<DraftRule>) {
    setRules((current) =>
      current.map((rule) => (rule.id === ruleId ? { ...rule, ...changes } : rule)),
    );
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate();
  }

  let error: string | undefined;
  if (save.isError) {
    error =
      isApiError(save.error) && save.error.status === 422
        ? t("pages.roleMapping.duplicate")
        : describeApiError(save.error, t).title;
  }
  const providerOptions = [{ key: "", label: t("pages.roleMapping.allProviders") }, ...providers];

  return (
    <form onSubmit={onSubmit}>
      <AdminSection id={`${id}-general`} title={t("pages.roleMapping.general")}>
        <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
          <div className="min-w-0">
            <div id={`${id}-enabled`} className="text-ui font-medium">
              {t("pages.roleMapping.enabled")}
            </div>
            <div className="text-ui text-muted-foreground">
              {t("pages.roleMapping.enabledDescription")}
            </div>
          </div>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            aria-labelledby={`${id}-enabled`}
            className="w-full shrink-0 sm:w-40"
            value={enabled ? "on" : "off"}
            onValueChange={(value) => value && setEnabled(value === "on")}
          >
            <ToggleGroupItem value="off" className="flex-1">
              {t("pages.roleMapping.off")}
            </ToggleGroupItem>
            <ToggleGroupItem value="on" className="flex-1">
              {t("pages.roleMapping.on")}
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
          <div className="min-w-0">
            <label htmlFor={`${id}-default`} className="text-ui font-medium">
              {t("pages.roleMapping.defaultRole")}
            </label>
            <div className="text-ui text-muted-foreground">
              {t("pages.roleMapping.defaultRoleDescription")}
            </div>
          </div>
          <NativeSelect
            id={`${id}-default`}
            size="sm"
            className="w-full shrink-0 sm:w-40"
            value={defaultRole}
            onChange={(event) => setDefaultRole(event.target.value as Role)}
          >
            {roles.map((role) => (
              <NativeSelectOption key={role} value={role}>
                {t(`account.roles.${role}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      </AdminSection>

      <AdminSection
        id={`${id}-rules`}
        title={t("pages.roleMapping.rules")}
        description={t("pages.roleMapping.rulesHint")}
        className="mt-8"
      >
        {rules.length === 0 && (
          <p className="px-4 py-3.5 text-ui text-muted-foreground">
            {t("pages.roleMapping.noRules")}
          </p>
        )}
        {rules.map((rule, index) => (
          <fieldset
            key={rule.id}
            className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-2 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_10rem_10rem_auto] sm:items-center"
          >
            <legend className="sr-only">
              {t("pages.roleMapping.rules")} {index + 1}
            </legend>
            <Input
              aria-label={t("pages.roleMapping.group")}
              placeholder={t("pages.roleMapping.group")}
              className="col-span-2 h-8 sm:col-span-1"
              value={rule.group}
              onChange={(event) => update(rule.id, { group: event.target.value })}
            />
            {/* Narrow screens: group and delete in the first row, provider and role below. */}
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              className="sm:order-last"
              aria-label={t("pages.roleMapping.removeRule")}
              onClick={() => setRules((current) => current.filter((r) => r.id !== rule.id))}
            >
              <Trash2 aria-hidden />
            </Button>
            <NativeSelect
              aria-label={t("pages.roleMapping.provider")}
              size="sm"
              className="w-full"
              value={rule.provider}
              onChange={(event) => update(rule.id, { provider: event.target.value })}
            >
              {providerOptions.map((option) => (
                <NativeSelectOption key={option.key} value={option.key}>
                  {option.label}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label={t("pages.roleMapping.role")}
              size="sm"
              className="col-span-2 w-full sm:col-span-1"
              value={rule.role}
              onChange={(event) => update(rule.id, { role: event.target.value as Role })}
            >
              {roles.map((role) => (
                <NativeSelectOption key={role} value={role}>
                  {t(`account.roles.${role}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </fieldset>
        ))}
        <div className="px-4 py-2.5">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() =>
              setRules((current) => [
                ...current,
                { id: newId(), group: "", provider: "", role: "admin" },
              ])
            }
          >
            <Plus aria-hidden />
            {t("pages.roleMapping.addRule")}
          </Button>
        </div>
      </AdminSection>

      <div className="mt-4 flex flex-col gap-3">
        {error && <Notice tone="error">{error}</Notice>}
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-ui text-muted-foreground">{t("pages.roleMapping.lastAdminHint")}</p>
          <Button type="submit" className="shrink-0" disabled={save.isPending}>
            {save.isPending ? t("pages.roleMapping.saving") : t("pages.roleMapping.save")}
          </Button>
        </div>
      </div>
    </form>
  );
}

function MappingTest({ providers }: { providers: ProviderOption[] }) {
  const { t } = useTranslation();
  const id = useId();
  const [provider, setProvider] = useState(providers[0]?.key ?? "");
  const [groups, setGroups] = useState("");
  const test = useMutation({
    mutationFn: () =>
      testRoleMapping(
        provider,
        groups
          .split("\n")
          .map((group) => group.trim())
          .filter(Boolean),
      ),
  });

  if (providers.length === 0) return null;
  const result = test.data;
  return (
    <AdminSection
      id={`${id}-test`}
      title={t("pages.roleMapping.test")}
      description={t("pages.roleMapping.testDescription")}
      className="mt-10"
    >
      <form
        className="flex flex-col gap-3 px-4 py-3.5"
        onSubmit={(event) => {
          event.preventDefault();
          test.mutate();
        }}
      >
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${id}-provider`} className="text-ui">
            {t("pages.roleMapping.provider")}
          </Label>
          <NativeSelect
            id={`${id}-provider`}
            size="sm"
            className="w-full sm:w-64"
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
          >
            {providers.map((option) => (
              <NativeSelectOption key={option.key} value={option.key}>
                {option.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${id}-groups`} className="text-ui">
            {t("pages.roleMapping.testGroups")}
          </Label>
          <textarea
            id={`${id}-groups`}
            rows={3}
            value={groups}
            onChange={(event) => setGroups(event.target.value)}
            className="w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 dark:bg-input/30"
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" size="sm" variant="outline" disabled={test.isPending}>
            {t("pages.roleMapping.testRun")}
          </Button>
          {result && (
            <div aria-live="polite" className="text-ui">
              {result.role ? (
                <>
                  <span className="font-medium">
                    {t("pages.roleMapping.testResult", { role: t(`account.roles.${result.role}`) })}
                  </span>
                  <span className="text-muted-foreground">
                    {" · "}
                    {result.matched_groups.length
                      ? t("pages.roleMapping.testMatched", {
                          groups: result.matched_groups.join(", "),
                        })
                      : t("pages.roleMapping.testNoMatch")}
                  </span>
                </>
              ) : (
                <span className="text-muted-foreground">{t("pages.roleMapping.testOff")}</span>
              )}
            </div>
          )}
        </div>
      </form>
    </AdminSection>
  );
}
