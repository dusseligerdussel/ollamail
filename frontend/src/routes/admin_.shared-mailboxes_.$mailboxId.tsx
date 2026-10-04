import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Pause, Play, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { type FormEvent, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  adminUsersQueryOptions,
  githubProvidersQueryOptions,
  ldapDirectoriesQueryOptions,
  oidcProvidersQueryOptions,
} from "@/api/admin-auth";
import { describeApiError, isApiError } from "@/api/errors";
import { problemErrorCode } from "@/api/mail";
import { isReauthCancelled } from "@/api/reauth";
import { SCIM_PROVIDER } from "@/api/scim";
import {
  deleteSharedMailbox,
  type GroupAssignment,
  type AssignmentPermission as Permission,
  type SharedMailbox,
  setAssignments,
  sharedMailboxQueryOptions,
  syncSharedMailbox,
  updateSharedMailbox,
} from "@/api/shared-mailboxes";
import { AdminSection, AdminSubPage } from "@/components/admin/admin-page";
import { ConfirmDialog } from "@/components/admin/confirm-dialog";
import { Notice } from "@/components/admin/notice";
import { useReauth } from "@/components/auth/reauth";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { SyncStatus } from "@/components/mail/sync-status";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin_/shared-mailboxes_/$mailboxId")({
  component: SharedMailboxPage,
});

function SharedMailboxPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const { mailboxId } = Route.useParams();
  const mailbox = useQuery({ ...sharedMailboxQueryOptions(mailboxId), enabled: isAdmin });
  if (!isAdmin) return <Forbidden />;
  return (
    <AdminSubPage
      title={mailbox.data?.display_name ?? t("pages.sharedMailboxes.title")}
      backLabel={t("pages.sharedMailboxes.backToList")}
      backTo="/admin/shared-mailboxes"
    >
      {mailbox.isPending && <ListSkeleton rows={6} />}
      {mailbox.isError &&
        (isApiError(mailbox.error) && mailbox.error.status === 404 ? (
          <Notice tone="error">{t("pages.sharedMailboxes.notFound")}</Notice>
        ) : (
          <InlineError
            error={mailbox.error}
            onRetry={mailbox.refetch}
            retrying={mailbox.isFetching}
          />
        ))}
      {mailbox.data && (
        <div className="flex flex-col gap-8">
          <StatusSection mailbox={mailbox.data} />
          {/* Re-mount the editor when the saved assignments change. */}
          <AccessForm key={JSON.stringify(mailbox.data.assignments)} mailbox={mailbox.data} />
          <RemoveSection mailbox={mailbox.data} />
        </div>
      )}
    </AdminSubPage>
  );
}

function StatusSection({ mailbox }: { mailbox: SharedMailbox }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["mailbox"] });
  const sync = useMutation({
    mutationFn: () => syncSharedMailbox(mailbox.id),
    onSuccess: () => {
      toast.success(t("pages.sharedMailboxes.syncRequested"));
      void refresh();
    },
  });
  const pause = useMutation({
    mutationFn: (enabled: boolean) => updateSharedMailbox(mailbox.id, { sync_enabled: enabled }),
    onSuccess: () => void refresh(),
  });
  return (
    <AdminSection id="shared-status" title={t("pages.sharedMailboxes.status")}>
      <div className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-ui font-medium">{mailbox.address}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {t(`mailboxes.types.${mailbox.type}`)}
            </span>
          </div>
          <SyncStatus status={mailbox.status} className="mt-0.5" />
        </div>
        <div className="flex shrink-0 gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!mailbox.sync_enabled || sync.isPending}
            onClick={() => sync.mutate()}
          >
            <RefreshCw />
            {t("pages.sharedMailboxes.syncNow")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={pause.isPending}
            onClick={() => pause.mutate(!mailbox.sync_enabled)}
            aria-label={
              mailbox.sync_enabled
                ? t("pages.sharedMailboxes.pause")
                : t("pages.sharedMailboxes.resume")
            }
          >
            {mailbox.sync_enabled ? <Pause /> : <Play />}
          </Button>
        </div>
      </div>
    </AdminSection>
  );
}

interface ProviderOption {
  key: string;
  label: string;
}

function groupKey(group: GroupAssignment) {
  return `${group.group.toLowerCase()}\u0000${group.provider ?? ""}`;
}

function assignmentsKey(users: Map<string, Permission>, groups: GroupAssignment[]) {
  return [
    ...[...users].map(([id, permission]) => `${id}\u0000${permission}`),
    ...groups.map((group) => `${groupKey(group)}\u0000${group.permission}`),
  ]
    .sort()
    .join("|");
}

/** `read`, or `act`: may also archive, move, trash and flag mails (never send). */
function PermissionSelect({
  value,
  onChange,
  label,
}: {
  value: Permission;
  onChange: (permission: Permission) => void;
  label: string;
}) {
  const { t } = useTranslation();
  return (
    <NativeSelect
      size="sm"
      className="w-40 shrink-0"
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value as Permission)}
    >
      <NativeSelectOption value="read">
        {t("pages.sharedMailboxes.permissions.read")}
      </NativeSelectOption>
      <NativeSelectOption value="act">
        {t("pages.sharedMailboxes.permissions.act")}
      </NativeSelectOption>
    </NativeSelect>
  );
}

function AccessForm({ mailbox }: { mailbox: SharedMailbox }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [users, oidc, github, ldap] = useQueries({
    queries: [
      adminUsersQueryOptions,
      oidcProvidersQueryOptions,
      githubProvidersQueryOptions,
      ldapDirectoriesQueryOptions,
    ],
  });
  const savedUsers = new Map(
    mailbox.assignments.flatMap((a) => (a.user_id ? [[a.user_id, a.permission] as const] : [])),
  );
  const savedGroups = mailbox.assignments.flatMap((a) =>
    a.group ? [{ group: a.group, provider: a.provider ?? null, permission: a.permission }] : [],
  );
  const [selected, setSelected] = useState(() => new Map(savedUsers));
  const [groups, setGroups] = useState<GroupAssignment[]>(savedGroups);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<string>();
  const filterId = useId();

  const providers: ProviderOption[] = [
    ...(oidc.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(github.data ?? []).map((p) => ({ key: p.provider, label: p.display_name })),
    ...(ldap.data ?? []).map((d) => ({ key: d.provider, label: d.display_name })),
    { key: SCIM_PROVIDER, label: t("pages.scim.providerLabel") },
  ];
  const visibleUsers = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (users.data ?? []).filter(
      (user) =>
        !needle ||
        user.display_name.toLowerCase().includes(needle) ||
        user.email.toLowerCase().includes(needle),
    );
  }, [users.data, filter]);

  const dirty = assignmentsKey(selected, groups) !== assignmentsKey(savedUsers, savedGroups);

  const withReauth = useReauth({
    action: t("auth.reauth.actions.saveMailboxAccess"),
    unsavedChanges: true,
  });
  const save = useMutation({
    // Needs a recent confirmation of the account (components/auth/reauth.tsx).
    mutationFn: () =>
      withReauth(() =>
        setAssignments(mailbox.id, {
          users: [...selected.keys()],
          act_users: [...selected].flatMap(([id, permission]) =>
            permission === "act" ? [id] : [],
          ),
          groups,
        }),
      ),
    meta: { errorToast: false },
    onSuccess: () => {
      toast.success(t("pages.sharedMailboxes.saved"));
      void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
    },
    onError: (failure) => {
      if (isReauthCancelled(failure)) return;
      setError(
        problemErrorCode(failure) === "unknown_user"
          ? t("pages.sharedMailboxes.unknownUser")
          : describeApiError(failure, t).title,
      );
    },
  });

  const assign = (userId: string, permission: Permission | undefined) => {
    setError(undefined);
    setSelected((current) => {
      const next = new Map(current);
      if (permission) next.set(userId, permission);
      else next.delete(userId);
      return next;
    });
  };

  return (
    <section aria-labelledby="shared-access" className="flex flex-col gap-4">
      <div>
        <h2 id="shared-access" className="text-xs font-medium text-muted-foreground">
          {t("pages.sharedMailboxes.access")}
        </h2>
        <p className="mt-1 text-ui text-muted-foreground">
          {t("pages.sharedMailboxes.accessDescription")}
        </p>
        <p className="mt-1 text-ui text-muted-foreground">
          {t("pages.sharedMailboxes.permissionsDescription")}
        </p>
        <p className="mt-1 text-ui">
          {mailbox.assignments.length === 0
            ? t("pages.sharedMailboxes.notAssigned")
            : t("pages.sharedMailboxes.readers", { count: mailbox.reader_count })}
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-ui font-medium">{t("pages.sharedMailboxes.users")}</h3>
          <Label htmlFor={filterId} className="sr-only">
            {t("pages.sharedMailboxes.searchUsers")}
          </Label>
          <Input
            id={filterId}
            type="search"
            className="h-8 max-w-56"
            placeholder={t("pages.sharedMailboxes.searchUsers")}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
        </div>
        {users.isPending && <ListSkeleton rows={3} />}
        {users.isError && (
          <InlineError error={users.error} onRetry={users.refetch} retrying={users.isFetching} />
        )}
        {users.data && (
          <ul
            aria-label={t("pages.sharedMailboxes.users")}
            className="max-h-72 divide-y overflow-y-auto rounded-lg border"
          >
            {visibleUsers.length === 0 && (
              <li className="px-4 py-3 text-ui text-muted-foreground">
                {t("pages.sharedMailboxes.noUsers")}
              </li>
            )}
            {visibleUsers.map((user) => {
              const id = `assign-${user.id}`;
              const permission = selected.get(user.id);
              return (
                <li key={user.id} className="flex items-center gap-3 px-4 py-2">
                  <Checkbox
                    id={id}
                    checked={!!permission}
                    onCheckedChange={(checked) =>
                      assign(user.id, checked === true ? (permission ?? "read") : undefined)
                    }
                  />
                  <Label htmlFor={id} className="flex min-w-0 flex-1 flex-col items-start gap-0">
                    <span className="truncate text-ui">{user.display_name}</span>
                    <span className="truncate text-xs font-normal text-muted-foreground">
                      {user.email}
                    </span>
                  </Label>
                  {permission && (
                    <PermissionSelect
                      value={permission}
                      onChange={(next) => assign(user.id, next)}
                      label={t("pages.sharedMailboxes.permissionFor", { name: user.display_name })}
                    />
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <GroupEditor
        groups={groups}
        providers={providers}
        onChange={(next) => {
          setError(undefined);
          setGroups(next);
        }}
      />

      {error && <Notice tone="error">{error}</Notice>}
      <div className="flex items-center justify-end gap-3">
        {dirty && (
          <span className="text-xs text-muted-foreground">
            {t("pages.sharedMailboxes.unsaved")}
          </span>
        )}
        <Button size="sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? t("pages.sharedMailboxes.saving") : t("pages.sharedMailboxes.save")}
        </Button>
      </div>
    </section>
  );
}

function GroupEditor({
  groups,
  providers,
  onChange,
}: {
  groups: GroupAssignment[];
  providers: ProviderOption[];
  onChange: (groups: GroupAssignment[]) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("");
  const nameId = useId();
  const providerId = useId();
  const label = (key: string | null | undefined) =>
    key
      ? (providers.find((p) => p.key === key)?.label ?? key)
      : t("pages.sharedMailboxes.anyProvider");

  const add = (event: FormEvent) => {
    event.preventDefault();
    const group: GroupAssignment = {
      group: name.trim(),
      provider: provider || null,
      permission: "read",
    };
    if (!group.group) return;
    if (!groups.some((existing) => groupKey(existing) === groupKey(group))) {
      onChange([...groups, group]);
    }
    setName("");
  };

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-ui font-medium">{t("pages.sharedMailboxes.groups")}</h3>
      <p className="text-ui text-muted-foreground">
        {t("pages.sharedMailboxes.groupsDescription")}
      </p>
      <ul aria-label={t("pages.sharedMailboxes.groups")} className="divide-y rounded-lg border">
        {groups.length === 0 && (
          <li className="px-4 py-3 text-ui text-muted-foreground">
            {t("pages.sharedMailboxes.noGroups")}
          </li>
        )}
        {groups.map((group) => (
          <li key={groupKey(group)} className="flex items-center gap-3 py-1.5 pr-2 pl-4">
            <span className="min-w-0 flex-1 truncate font-mono text-ui">{group.group}</span>
            <span className="min-w-0 shrink truncate text-xs text-muted-foreground">
              {label(group.provider)}
            </span>
            <PermissionSelect
              value={group.permission}
              onChange={(permission) =>
                onChange(
                  groups.map((g) => (groupKey(g) === groupKey(group) ? { ...g, permission } : g)),
                )
              }
              label={t("pages.sharedMailboxes.permissionFor", { name: group.group })}
            />
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label={t("pages.sharedMailboxes.removeGroup", { group: group.group })}
              onClick={() => onChange(groups.filter((g) => groupKey(g) !== groupKey(group)))}
            >
              <X />
            </Button>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="grid gap-2 sm:grid-cols-[1fr_11rem_auto] sm:items-end">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={nameId} className="text-xs text-muted-foreground">
            {t("pages.sharedMailboxes.groupName")}
          </Label>
          <Input
            id={nameId}
            className="h-8"
            spellCheck={false}
            autoComplete="off"
            maxLength={255}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={providerId} className="text-xs text-muted-foreground">
            {t("pages.sharedMailboxes.provider")}
          </Label>
          <NativeSelect
            id={providerId}
            size="sm"
            className="w-full"
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
          >
            <NativeSelectOption value="">
              {t("pages.sharedMailboxes.anyProvider")}
            </NativeSelectOption>
            {providers.map((option) => (
              <NativeSelectOption key={option.key} value={option.key}>
                {option.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <Button type="submit" size="sm" variant="outline" disabled={!name.trim()}>
          <Plus />
          {t("pages.sharedMailboxes.addGroup")}
        </Button>
      </form>
    </div>
  );
}

function RemoveSection({ mailbox }: { mailbox: SharedMailbox }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: () => deleteSharedMailbox(mailbox.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
      void queryClient.invalidateQueries({ queryKey: ["message"] });
      toast.success(t("pages.sharedMailboxes.removed"));
      void navigate({ to: "/admin/shared-mailboxes" });
    },
  });
  const count = mailbox.status.message_count;
  return (
    <div className="flex justify-start border-t pt-6">
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Trash2 />
        {t("pages.sharedMailboxes.remove")}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={t("pages.sharedMailboxes.removeTitle")}
        description={t("pages.sharedMailboxes.removeDescription", {
          name: mailbox.display_name,
          count,
          formatted: new Intl.NumberFormat(i18n.language).format(count),
        })}
        confirmLabel={t("pages.sharedMailboxes.remove")}
        cancelLabel={t("pages.sharedMailboxes.cancel")}
        pending={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
    </div>
  );
}
