import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Ellipsis, UserPlus, Users } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  type AdminUser,
  adminUsersQueryOptions,
  authSettingsQueryOptions,
  githubProvidersQueryOptions,
  type InvitationIssued,
  isAdminLockout,
  ldapDirectoriesQueryOptions,
  oidcProvidersQueryOptions,
  providerKind,
  reissueInvitation,
  revokeUserSessions,
  roleMappingQueryOptions,
  updateUser,
} from "@/api/admin-auth";
import { describeApiError } from "@/api/errors";
import { AdminSubPage } from "@/components/admin/admin-page";
import { ConfirmDialog } from "@/components/admin/confirm-dialog";
import { InvitationLink, InviteUserSheet } from "@/components/admin/invite-user-sheet";
import { EmptyState } from "@/components/empty-state";
import { Forbidden } from "@/components/forbidden";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useCurrentUser } from "@/hooks/use-current-user";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";

export const Route = createFileRoute("/admin_/users")({
  component: UsersPage,
});

function UsersPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const [inviting, setInviting] = useState(false);
  const users = useQuery({ ...adminUsersQueryOptions, enabled: isAdmin });
  if (!isAdmin) return <Forbidden />;

  return (
    <AdminSubPage
      title={t("pages.users.title")}
      backLabel={t("pages.users.back")}
      meta={users.data ? t("pages.users.count", { count: users.data.length }) : undefined}
      actions={
        <Button size="sm" onClick={() => setInviting(true)}>
          <UserPlus aria-hidden />
          {t("pages.users.invite")}
        </Button>
      }
      wide
    >
      <UserList onInvite={() => setInviting(true)} />
      <InviteUserSheet open={inviting} onOpenChange={setInviting} />
    </AdminSubPage>
  );
}

function UserList({ onInvite }: { onInvite: () => void }) {
  const { t } = useTranslation();
  const wide = useMediaQuery(mediaQueries.sidebar);
  const users = useQuery(adminUsersQueryOptions);
  const mapping = useQuery(roleMappingQueryOptions);
  const actions = useUserActions();

  if (users.isPending) return <ListSkeleton />;
  if (!users.data) return <InlineError error={users.error} className="px-4 py-4 md:px-5" />;
  if (users.data.length === 0) {
    return (
      <EmptyState
        icon={Users}
        title={t("pages.users.title")}
        action={<Button onClick={onInvite}>{t("pages.users.invite")}</Button>}
      />
    );
  }

  return (
    <>
      <div className="flex flex-col gap-2 border-b px-4 py-2.5 text-ui text-muted-foreground md:px-5">
        <p>{t("pages.users.noMailAccess")}</p>
        {mapping.data?.enabled && <p>{t("pages.users.roleManaged")}</p>}
      </div>
      {wide ? (
        <UserTable users={users.data} actions={actions} />
      ) : (
        <UserCards users={users.data} actions={actions} />
      )}
      {actions.dialogs}
    </>
  );
}

type Action =
  | { type: "role"; user: AdminUser }
  | { type: "active"; user: AdminUser }
  | { type: "sessions"; user: AdminUser }
  | { type: "invitation"; user: AdminUser };

function useUserActions() {
  const { t } = useTranslation();
  const me = useCurrentUser();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<Action>();
  const [issued, setIssued] = useState<InvitationIssued>();

  const run = useMutation({
    mutationFn: async (action: Action) => {
      const { user } = action;
      switch (action.type) {
        case "role":
          return updateUser(user.id, { role: user.role === "admin" ? "user" : "admin" });
        case "active":
          return updateUser(user.id, { is_active: !user.is_active });
        case "sessions":
          return revokeUserSessions(user.id);
        case "invitation":
          return reissueInvitation(user.id);
      }
    },
    meta: { errorToast: false },
    onSuccess: async (result, action) => {
      setConfirm(undefined);
      if (action.type === "invitation") setIssued(result as InvitationIssued);
      else
        toast.success(
          t(action.type === "sessions" ? "pages.users.sessionsRevoked" : "pages.users.updated"),
        );
      // Losing the own admin role: the API now answers 403; reload the current user.
      if (action.user.id === me.id)
        await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      await queryClient.invalidateQueries({ queryKey: adminUsersQueryOptions.queryKey });
      await queryClient.invalidateQueries({ queryKey: authSettingsQueryOptions.queryKey });
    },
    onError: (error) => {
      // Inside the confirmation dialog the error is shown there.
      if (confirm) return;
      toast.error(
        isAdminLockout(error) ? t("pages.users.lockout") : describeApiError(error, t).title,
      );
    },
  });

  /** Changes to the own account that remove access are confirmed first. */
  function start(action: Action) {
    run.reset();
    const self = action.user.id === me.id;
    const removesAccess =
      (action.type === "role" && action.user.role === "admin") ||
      (action.type === "active" && action.user.is_active);
    if (self && removesAccess) setConfirm(action);
    else run.mutate(action);
  }

  let confirmError: string | undefined;
  if (confirm && run.isError) {
    confirmError = isAdminLockout(run.error)
      ? t("pages.users.lockout")
      : describeApiError(run.error, t).title;
  }

  const dialogs = (
    <>
      <ConfirmDialog
        open={confirm !== undefined}
        onOpenChange={(open) => !open && setConfirm(undefined)}
        title={
          confirm?.type === "active"
            ? t("pages.users.self.deactivateTitle")
            : t("pages.users.self.demoteTitle")
        }
        warning={t("pages.users.self.warning")}
        error={confirmError}
        confirmLabel={t("pages.users.self.confirm")}
        cancelLabel={t("pages.users.self.cancel")}
        pending={run.isPending}
        onConfirm={() => confirm && run.mutate(confirm)}
      />
      <Dialog open={issued !== undefined} onOpenChange={(open) => !open && setIssued(undefined)}>
        <DialogContent closeLabel={t("common.close")}>
          <DialogHeader>
            <DialogTitle className="text-base">{t("pages.users.newInvitation")}</DialogTitle>
            <DialogDescription className="text-ui">{issued?.user.email}</DialogDescription>
          </DialogHeader>
          {issued && <InvitationLink issued={issued} />}
          <DialogFooter>
            <Button onClick={() => setIssued(undefined)}>
              {t("pages.users.inviteSheet.done")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );

  return { start, pending: run.isPending, dialogs, meId: me.id };
}

type UserActions = ReturnType<typeof useUserActions>;

function useFormatters() {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  // Already loaded on the sign-in pages; only used for readable provider names.
  const oidc = useQuery(oidcProvidersQueryOptions);
  const github = useQuery(githubProvidersQueryOptions);
  const ldap = useQuery(ldapDirectoriesQueryOptions);
  const names = new Map<string, string>([
    ...(oidc.data ?? []).map((p) => [p.provider, p.display_name] as const),
    ...(github.data ?? []).map((p) => [p.provider, p.display_name] as const),
    ...(ldap.data ?? []).map((d) => [d.provider, d.display_name] as const),
  ]);
  const date = new Intl.DateTimeFormat(i18n.resolvedLanguage, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: timezone,
  });
  return {
    lastLogin: (user: AdminUser) =>
      user.last_login_at ? date.format(new Date(user.last_login_at)) : t("pages.users.never"),
    provider: (key: string) => {
      if (providerKind(key) === "local") return t("pages.signIn.kinds.local");
      return names.get(key) ?? key;
    },
  };
}

function StatusBadge({ user }: { user: AdminUser }) {
  const { t } = useTranslation();
  if (!user.is_active) {
    return (
      <Badge variant="outline" className="font-normal text-muted-foreground">
        {t("pages.users.status.inactive")}
      </Badge>
    );
  }
  if (user.invitation_pending) {
    return (
      <Badge variant="outline" className="font-normal">
        {t("pages.users.status.invited")}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="font-normal">
      {t("pages.users.status.active")}
    </Badge>
  );
}

function Providers({ user }: { user: AdminUser }) {
  const format = useFormatters();
  return (
    <span className="flex flex-wrap gap-1">
      {user.providers.map((key) => (
        <Badge key={key} variant="outline" className="font-normal" title={key}>
          {format.provider(key)}
        </Badge>
      ))}
    </span>
  );
}

function UserMenu({ user, actions }: { user: AdminUser; actions: UserActions }) {
  const { t } = useTranslation();
  const canReinvite = user.is_active && user.providers.length === 0;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          size="icon-sm"
          variant="ghost"
          disabled={actions.pending}
          aria-label={t("pages.users.actionsFor", { name: user.display_name })}
        >
          <Ellipsis aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => actions.start({ type: "role", user })}>
          {user.role === "admin" ? t("pages.users.makeUser") : t("pages.users.makeAdmin")}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => actions.start({ type: "sessions", user })}>
          {t("pages.users.revokeSessions")}
        </DropdownMenuItem>
        {canReinvite && (
          <DropdownMenuItem onSelect={() => actions.start({ type: "invitation", user })}>
            {t("pages.users.newInvitation")}
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          variant={user.is_active ? "destructive" : "default"}
          onSelect={() => actions.start({ type: "active", user })}
        >
          {user.is_active ? t("pages.users.deactivate") : t("pages.users.reactivate")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function NameCell({ user, isMe }: { user: AdminUser; isMe: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="truncate font-medium">{user.display_name}</span>
        {isMe && (
          <Badge variant="secondary" className="shrink-0 font-normal">
            {t("pages.users.you")}
          </Badge>
        )}
      </div>
      <div className="truncate text-muted-foreground">{user.email}</div>
    </div>
  );
}

function UserTable({ users, actions }: { users: AdminUser[]; actions: UserActions }) {
  const { t } = useTranslation();
  const format = useFormatters();
  const headClass = "h-8 px-2 text-left text-xs font-medium text-muted-foreground first:pl-5";
  const cellClass = "px-2 py-2.5 first:pl-5 last:pr-5 align-middle";
  return (
    <table
      aria-label={t("pages.users.title")}
      className="w-full table-fixed border-collapse text-ui"
    >
      <thead className="sticky top-0 z-10 bg-background">
        <tr className="border-b">
          <th scope="col" className={headClass}>
            {t("pages.users.columns.user")}
          </th>
          <th scope="col" className={`${headClass} w-36`}>
            {t("pages.users.columns.role")}
          </th>
          <th scope="col" className={`${headClass} w-52`}>
            {t("pages.users.columns.signIn")}
          </th>
          <th scope="col" className={`${headClass} w-32`}>
            {t("pages.users.columns.status")}
          </th>
          <th scope="col" className={`${headClass} w-48`}>
            {t("pages.users.columns.lastLogin")}
          </th>
          <th scope="col" className={`${headClass} w-14`}>
            <span className="sr-only">{t("pages.users.columns.actions")}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {users.map((user) => (
          <tr key={user.id} className="border-b border-border/60">
            <td className={cellClass}>
              <NameCell user={user} isMe={user.id === actions.meId} />
            </td>
            <td className={cellClass}>{t(`account.roles.${user.role}`)}</td>
            <td className={cellClass}>
              <Providers user={user} />
            </td>
            <td className={cellClass}>
              <StatusBadge user={user} />
            </td>
            <td className={`${cellClass} text-muted-foreground tabular-nums`}>
              <div>{format.lastLogin(user)}</div>
              {user.active_sessions > 0 && (
                <div className="text-xs">
                  {t("pages.users.sessions", { count: user.active_sessions })}
                </div>
              )}
            </td>
            <td className={`${cellClass} text-right`}>
              <UserMenu user={user} actions={actions} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function UserCards({ users, actions }: { users: AdminUser[]; actions: UserActions }) {
  const { t } = useTranslation();
  const format = useFormatters();
  return (
    <ul aria-label={t("pages.users.title")} className="flex flex-col">
      {users.map((user) => (
        <li
          key={user.id}
          className="flex items-start gap-2 border-b border-border/60 px-4 py-3 text-ui"
        >
          <div className="min-w-0 flex-1">
            <NameCell user={user} isMe={user.id === actions.meId} />
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <StatusBadge user={user} />
              <span className="text-muted-foreground">{t(`account.roles.${user.role}`)}</span>
              <Providers user={user} />
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {t("pages.users.columns.lastLogin")}: {format.lastLogin(user)}
            </div>
          </div>
          <UserMenu user={user} actions={actions} />
        </li>
      ))}
    </ul>
  );
}
