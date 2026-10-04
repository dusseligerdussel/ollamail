import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "@tanstack/react-router";
import { ChevronDown, Cloud, KeyRound, type LucideIcon, TriangleAlert, X } from "lucide-react";
import { type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { aiStatusQueryOptions } from "@/api/ai";
import { dismissLinkNotice, linkNoticesQueryOptions } from "@/api/auth";
import { mailboxesQueryOptions } from "@/api/mail";
import { modelStatusQueryOptions } from "@/api/system";
import { useDateFormat } from "@/components/account/sessions-list";
import { useMailErrorText } from "@/components/mail/sync-status";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import { cn } from "@/lib/utils";

interface Notice {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Something does not work (red icon) or plain information. */
  warning: boolean;
  content: ReactNode;
  dismissLabel?: string;
  onDismiss?: () => void;
}

/**
 * Unobtrusive notices above the content: a sign-in was linked to the account by e-mail address
 * (everyone, #208), a cloud provider receives mail content (everyone, docs/PRIVACY.md), one of the
 * user's own mailboxes cannot sync (everyone), or a model is missing or the LLM endpoint is
 * unreachable (admins). On narrow screens they are folded into one line
 * that expands. Renders nothing without notices.
 */
export function SystemNotices() {
  const { t } = useTranslation();
  const wide = useMediaQuery(mediaQueries.sidebar);
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const notices = [
    useLinkNotice(),
    useCloudNotice(),
    useMailboxErrorNotice(),
    useModelNotice(),
  ].filter((notice): notice is Notice => notice !== null);

  if (notices.length === 0) return null;
  if (wide) {
    return notices.map((notice) => (
      <aside key={notice.id} aria-label={notice.label} className={barClass}>
        <NoticeContent notice={notice} />
      </aside>
    ));
  }

  const [first] = notices;
  const Icon = notices.length === 1 && first ? first.icon : TriangleAlert;
  return (
    <aside aria-label={t("shell.notices.label")} className="shrink-0 border-b bg-muted/50">
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setExpanded((value) => !value)}
        className="flex min-h-7 w-full items-center gap-2 px-4 py-1.5 text-left text-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80 focus-visible:ring-inset"
      >
        <Icon
          aria-hidden
          className={cn(
            "size-3.5 shrink-0",
            notices.some((notice) => notice.warning) ? "text-destructive" : "text-muted-foreground",
          )}
        />
        <span className="min-w-0 flex-1 truncate font-medium">
          {notices.length === 1 && first
            ? first.label
            : t("shell.notices.summary", { count: notices.length })}
        </span>
        <ChevronDown
          aria-hidden
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
            expanded && "rotate-180",
          )}
        />
      </button>
      <ul id={listId} hidden={!expanded} className="divide-y border-t">
        {notices.map((notice) => (
          <li key={notice.id} className="flex items-start gap-2 px-4 py-1.5">
            <NoticeContent notice={notice} />
          </li>
        ))}
      </ul>
    </aside>
  );
}

const barClass =
  "flex shrink-0 items-start gap-2 border-b bg-muted/50 px-4 py-1.5 text-xs text-muted-foreground md:px-5";

function NoticeContent({ notice }: { notice: Notice }) {
  const Icon = notice.icon;
  return (
    <>
      <Icon
        aria-hidden
        className={cn("mt-px size-3.5 shrink-0", notice.warning && "text-destructive")}
      />
      <p className="min-w-0 flex-1 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{notice.label}</span>
        {" · "}
        {notice.content}
      </p>
      {notice.onDismiss && (
        <Button
          variant="ghost"
          size="icon-xs"
          className="-my-1 -mr-1.5 text-muted-foreground"
          aria-label={notice.dismissLabel}
          onClick={notice.onDismiss}
        >
          <X aria-hidden />
        </Button>
      )}
    </>
  );
}

const linkClass =
  "rounded-sm font-medium text-foreground underline underline-offset-2 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80";

/**
 * A sign-in was linked to the account by e-mail address (#208). The server only shows it to
 * sessions of other sign-in methods, so whoever uses the new link cannot hide it. Dismissing
 * confirms the link; otherwise the sessions list shows and ends the session it created.
 */
function useLinkNotice(): Notice | null {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formatDate = useDateFormat();
  const { data } = useQuery(linkNoticesQueryOptions);
  const dismiss = useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map(dismissLinkNotice)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: linkNoticesQueryOptions.queryKey }),
  });
  const oldest = data?.at(-1);
  if (!data || !oldest || dismiss.isPending) return null;

  const providers = [...new Set(data.map((notice) => notice.provider_name ?? notice.provider))];
  return {
    id: "link",
    label: t("shell.linkNotice.label"),
    icon: KeyRound,
    warning: true,
    content: (
      <>
        {t("shell.linkNotice.text", {
          count: data.length,
          providers: providers.join(", "),
          date: formatDate(oldest.created_at),
        })}{" "}
        <Link to="/settings" hash="settings-sessions" className={linkClass}>
          {t("shell.linkNotice.action")}
        </Link>
      </>
    ),
    dismissLabel: t("shell.linkNotice.dismiss"),
    onDismiss: () => dismiss.mutate(data.map((notice) => notice.id)),
  };
}

const cloudDismissedKey = "ollamail.cloudNotice.dismissed";

function readDismissed() {
  try {
    return sessionStorage.getItem(cloudDismissedKey);
  } catch {
    return null;
  }
}

/**
 * Which cloud provider receives mail content for which tasks. Purely informative, so it can be
 * hidden for the browser session; it returns as soon as providers or tasks change.
 */
function useCloudNotice(): Notice | null {
  const { t } = useTranslation();
  const { data } = useQuery(aiStatusQueryOptions);
  const [dismissed, setDismissed] = useState(readDismissed);
  if (!data || data.cloud.length === 0) return null;

  const signature = JSON.stringify(data.cloud);
  if (dismissed === signature) return null;
  return {
    id: "cloud",
    label: t("shell.cloudNotice.label"),
    icon: Cloud,
    warning: false,
    content: data.cloud
      .map((usage) =>
        t("shell.cloudNotice.text", {
          provider: usage.display_name,
          tasks: usage.tasks.map((task) => t(`pages.ai.tasks.${task}`)).join(", "),
        }),
      )
      .join(" · "),
    dismissLabel: t("shell.cloudNotice.dismiss"),
    onDismiss: () => {
      try {
        sessionStorage.setItem(cloudDismissedKey, signature);
      } catch {
        // Without storage it stays hidden until the page is reloaded.
      }
      setDismissed(signature);
    },
  };
}

function useMailboxErrorNotice(): Notice | null {
  const { t } = useTranslation();
  const errorText = useMailErrorText();
  const pathname = useLocation({ select: (location) => location.pathname });
  const { data } = useQuery(mailboxesQueryOptions);
  const failing = data?.filter((mailbox) => !mailbox.is_shared && mailbox.status.phase === "error");
  // The mailbox settings show the error next to each mailbox.
  if (!failing?.length || pathname.startsWith("/settings/mailboxes")) return null;

  const [first] = failing;
  return {
    id: "mailbox",
    label: t("shell.syncNotice.label"),
    icon: TriangleAlert,
    warning: true,
    content: (
      <>
        {first && failing.length === 1
          ? t("shell.syncNotice.one", {
              mailbox: first.display_name,
              reason: errorText(first.status.last_error),
            })
          : t("shell.syncNotice.many", { count: failing.length })}{" "}
        <Link to="/settings/mailboxes" className={linkClass}>
          {t("shell.syncNotice.action")}
        </Link>
      </>
    ),
  };
}

function useModelNotice(): Notice | null {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const pathname = useLocation({ select: (location) => location.pathname });
  const { data } = useQuery({ ...modelStatusQueryOptions, enabled: isAdmin });
  // The admin overview shows the details.
  if (!isAdmin || !data || pathname === "/admin") return null;

  const unreachable = [
    ...new Set(data.filter((m) => m.state === "unreachable").map((m) => m.endpoint)),
  ];
  const missing = [...new Set(data.filter((m) => m.state === "missing").map((m) => m.model))];
  if (unreachable.length === 0 && missing.length === 0) return null;

  return {
    id: "model",
    label:
      unreachable.length > 0 ? t("shell.modelNotice.unreachable") : t("shell.modelNotice.missing"),
    icon: TriangleAlert,
    warning: true,
    content: (
      <>
        {unreachable.length > 0
          ? t("shell.modelNotice.unreachableText", {
              endpoints: unreachable.join(", "),
              count: unreachable.length,
            })
          : t("shell.modelNotice.missingText", {
              models: missing.join(", "),
              count: missing.length,
            })}{" "}
        <Link to="/admin" className={linkClass}>
          {t("shell.modelNotice.action")}
        </Link>
      </>
    ),
  };
}
