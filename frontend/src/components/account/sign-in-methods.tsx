import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Ban, KeyRound } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  identitiesQueryOptions,
  type LinkBlock,
  liftLinkBlock,
  linkBlocksQueryOptions,
  linkNoticesQueryOptions,
  type SignInIdentity,
  sessionsQueryOptions,
  unlinkIdentity,
} from "@/api/auth";
import { isApiError } from "@/api/errors";
import { isReauthCancelled } from "@/api/reauth";
import {
  useDateFormat,
  useNewlyLinkedProviders,
  useSignInMethodName,
} from "@/components/account/sessions-list";
import { useReauth } from "@/components/auth/reauth";
import { InlineError } from "@/components/inline-error";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";

type Refusal = NonNullable<SignInIdentity["unlink_refusal"]>;

function useRefreshAccess() {
  const queryClient = useQueryClient();
  return () =>
    Promise.all(
      [
        identitiesQueryOptions,
        linkBlocksQueryOptions,
        sessionsQueryOptions,
        linkNoticesQueryOptions,
      ].map((options) => queryClient.invalidateQueries({ queryKey: options.queryKey })),
    );
}

function refusalText(refusal: Refusal, t: TFunction) {
  switch (refusal) {
    case "local":
      return t("account.signIn.refusal.local");
    case "current_session":
      return t("account.signIn.refusal.currentSession");
    case "last_sign_in":
      return t("account.signIn.refusal.lastSignIn");
  }
}

function IdentityRow({
  identity,
  newlyLinked,
  onUnlink,
}: {
  identity: SignInIdentity;
  newlyLinked: boolean;
  onUnlink: (identity: SignInIdentity) => void;
}) {
  const { t } = useTranslation();
  const formatDate = useDateFormat();
  const name = useSignInMethodName()(identity);
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <KeyRound className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-ui font-medium">{name}</span>
          {identity.current && (
            <Badge variant="secondary" className="shrink-0 font-normal">
              {t("account.signIn.current")}
            </Badge>
          )}
          {newlyLinked && (
            <Badge variant="outline" className="shrink-0 font-normal">
              {t("account.sessions.newlyLinked")}
            </Badge>
          )}
        </div>
        <div className="truncate text-ui text-muted-foreground">
          {identity.provider === "local"
            ? t("account.signIn.localDescription")
            : t("account.signIn.linked", { date: formatDate(identity.created_at) })}
        </div>
        {identity.last_used_at && (
          <div className="truncate text-ui text-muted-foreground">
            {t("account.signIn.lastUsed", { date: formatDate(identity.last_used_at) })}
          </div>
        )}
        {identity.unlink_refusal && identity.unlink_refusal !== "local" && (
          <div className="text-ui text-muted-foreground">
            {refusalText(identity.unlink_refusal, t)}
          </div>
        )}
      </div>
      {!identity.unlink_refusal && (
        <Button
          variant="ghost"
          size="sm"
          className="text-ui"
          onClick={() => onUnlink(identity)}
          aria-label={t("account.signIn.unlinkLabel", { method: name })}
        >
          {t("account.signIn.unlink")}
        </Button>
      )}
    </li>
  );
}

function BlockRow({ block }: { block: LinkBlock }) {
  const { t } = useTranslation();
  const formatDate = useDateFormat();
  const withReauth = useReauth();
  const refresh = useRefreshAccess();
  const name = useSignInMethodName()(block);
  const lift = useMutation({
    meta: { errorToast: false },
    mutationFn: () => withReauth(() => liftLinkBlock(block.id)),
    onSuccess: refresh,
  });
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <Ban className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-ui font-medium">{name}</span>
          <Badge variant="outline" className="shrink-0 font-normal">
            {t("account.signIn.blocked")}
          </Badge>
        </div>
        <div className="text-ui text-muted-foreground">
          {t("account.signIn.blockedDescription", { date: formatDate(block.created_at) })}
        </div>
        {lift.isError && !isReauthCancelled(lift.error) && (
          <InlineError error={lift.error} className="mt-1" />
        )}
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="text-ui"
        disabled={lift.isPending}
        onClick={() => lift.mutate()}
        aria-label={t("account.signIn.unblockLabel", { method: name })}
      >
        {t("account.signIn.unblock")}
      </Button>
    </li>
  );
}

function UnlinkDialog({
  identity,
  onOpenChange,
}: {
  identity: SignInIdentity | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const withReauth = useReauth();
  const refresh = useRefreshAccess();
  const providerName = useSignInMethodName();
  // Keeps the text while the dialog closes.
  const [shown, setShown] = useState(identity);
  if (identity && identity !== shown) setShown(identity);
  const unlink = useMutation({
    meta: { errorToast: false },
    mutationFn: (id: string) => withReauth(() => unlinkIdentity(id)),
    onSuccess: async () => {
      onOpenChange(false);
      await refresh();
    },
  });
  const name = shown ? providerName(shown) : "";
  const refusal =
    isApiError(unlink.error) && unlink.error.status === 409
      ? (unlink.error.problem?.reason as Refusal | undefined)
      : undefined;

  return (
    <Dialog
      open={identity !== null}
      onOpenChange={(open) => {
        if (!open) unlink.reset();
        onOpenChange(open);
      }}
    >
      <DialogContent closeLabel={t("common.close")}>
        <DialogHeader>
          <DialogTitle>{t("account.signIn.confirmTitle", { method: name })}</DialogTitle>
          <DialogDescription>
            {t("account.signIn.confirmDescription", { method: name })}
          </DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1 pl-5 text-ui text-muted-foreground">
          <li>{t("account.signIn.consequenceSessions", { method: name })}</li>
          <li>{t("account.signIn.consequenceBlock", { method: name })}</li>
          <li>{t("account.signIn.consequenceUndo")}</li>
        </ul>
        {unlink.isError &&
          !isReauthCancelled(unlink.error) &&
          (refusal ? (
            <p role="alert" className="text-ui text-destructive">
              {refusalText(refusal, t)}
            </p>
          ) : (
            <InlineError error={unlink.error} />
          ))}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">{t("account.signIn.cancel")}</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={!shown || unlink.isPending}
            onClick={() => shown && unlink.mutate(shown.id)}
          >
            {t("account.signIn.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The own ways to sign in (#216): external sign-ins can be unlinked, which ends their sessions
 * and blocks the provider from linking to the account by e-mail address again; blocked providers
 * are listed with the action to lift the block.
 */
export function SignInMethods() {
  const { t } = useTranslation();
  const identities = useQuery(identitiesQueryOptions);
  const blocks = useQuery(linkBlocksQueryOptions);
  const newlyLinked = useNewlyLinkedProviders();
  const [unlinking, setUnlinking] = useState<SignInIdentity | null>(null);

  if (identities.isPending || blocks.isPending) {
    return (
      <div role="status" aria-label={t("common.loading")} className="divide-y">
        {[0, 1].map((row) => (
          <div key={row} className="flex items-center gap-3 px-4 py-3">
            <Skeleton className="size-4" />
            <div className="flex flex-1 flex-col gap-1.5">
              <Skeleton className="h-3.5 w-32" />
              <Skeleton className="h-3.5 w-48" />
              <Skeleton className="h-3.5 w-40" />
            </div>
          </div>
        ))}
      </div>
    );
  }
  for (const query of [identities, blocks]) {
    if (query.isError) {
      return (
        <InlineError
          error={query.error}
          onRetry={query.refetch}
          retrying={query.isFetching}
          className="px-4 py-3.5"
        />
      );
    }
  }
  if (!identities.data || !blocks.data) return null;

  return (
    <>
      <ul className="divide-y">
        {identities.data.map((identity) => (
          <IdentityRow
            key={identity.id}
            identity={identity}
            newlyLinked={newlyLinked.has(identity.provider)}
            onUnlink={setUnlinking}
          />
        ))}
        {blocks.data.map((block) => (
          <BlockRow key={block.id} block={block} />
        ))}
      </ul>
      <UnlinkDialog identity={unlinking} onOpenChange={(open) => !open && setUnlinking(null)} />
    </>
  );
}
