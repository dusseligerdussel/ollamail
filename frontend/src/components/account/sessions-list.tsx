import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Monitor, Smartphone } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  type AuthSession,
  revokeOtherSessions,
  revokeSession,
  sessionsQueryOptions,
} from "@/api/auth";
import { InlineError } from "@/components/inline-error";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import { parseUserAgent } from "@/lib/user-agent";

export function useDateFormat() {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  return (value: string) => {
    try {
      return new Intl.DateTimeFormat(i18n.resolvedLanguage, {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: timezone,
      }).format(new Date(value));
    } catch {
      return new Date(value).toLocaleString(i18n.resolvedLanguage);
    }
  };
}

function SessionRow({ session }: { session: AuthSession }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formatDate = useDateFormat();
  const device = parseUserAgent(session.user_agent);
  const Icon = device.mobile ? Smartphone : Monitor;
  const revoke = useMutation({
    mutationFn: () => revokeSession(session.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQueryOptions.queryKey }),
  });

  let name = t("account.sessions.unknownDevice");
  if (device.browser && device.os) {
    name = t("account.sessions.device", { browser: device.browser, os: device.os });
  } else if (device.browser || device.os) {
    name = (device.browser ?? device.os) as string;
  }

  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-ui font-medium" title={session.user_agent ?? undefined}>
            {name}
          </span>
          {session.current && (
            <Badge variant="secondary" className="shrink-0 font-normal">
              {t("account.sessions.current")}
            </Badge>
          )}
        </div>
        <div className="truncate text-ui text-muted-foreground">
          {t("account.sessions.lastActive", { date: formatDate(session.last_seen_at) })}
        </div>
      </div>
      {!session.current && (
        <Button
          variant="ghost"
          size="sm"
          className="text-ui"
          disabled={revoke.isPending}
          onClick={() => revoke.mutate()}
          aria-label={t("account.sessions.revokeLabel", { device: name })}
        >
          {t("account.sessions.revoke")}
        </Button>
      )}
    </li>
  );
}

/** The own active sessions with sign-out per device. */
export function SessionsList() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const sessions = useQuery(sessionsQueryOptions);
  const revokeOthers = useMutation({
    mutationFn: revokeOtherSessions,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQueryOptions.queryKey }),
  });

  if (sessions.isPending) {
    return (
      <div role="status" aria-label={t("common.loading")} className="divide-y">
        {[0, 1].map((row) => (
          <div key={row} className="flex items-center gap-3 px-4 py-3">
            <Skeleton className="size-4" />
            <div className="flex flex-1 flex-col gap-1.5">
              <Skeleton className="h-3.5 w-40" />
              <Skeleton className="h-3.5 w-56" />
            </div>
          </div>
        ))}
      </div>
    );
  }
  if (sessions.isError) {
    return <InlineError error={sessions.error} className="px-4 py-3.5" />;
  }

  // This device first, then the others by last use (server order).
  const sorted = [...sessions.data].sort((a, b) => Number(b.current) - Number(a.current));
  const hasOthers = sorted.some((session) => !session.current);
  return (
    <>
      <ul className="divide-y">
        {sorted.map((session) => (
          <SessionRow key={session.id} session={session} />
        ))}
      </ul>
      {hasOthers && (
        <div className="flex justify-end px-4 py-3">
          <Button
            variant="outline"
            size="sm"
            className="text-ui"
            disabled={revokeOthers.isPending}
            onClick={() => revokeOthers.mutate()}
          >
            {t("account.sessions.revokeOthers")}
          </Button>
        </div>
      )}
    </>
  );
}
