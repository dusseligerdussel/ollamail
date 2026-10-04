import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { FilePen, Trash2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { type Draft, discardDraft, draftKeys, openDraftsQueryOptions } from "@/api/drafts";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { useListNavigation } from "@/hooks/use-list-navigation";
import type { Command } from "@/lib/commands";
import { addressName, formatListDate } from "@/lib/mail-format";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/drafts")({
  component: DraftsPage,
});

/** Overview of the open reply drafts (#93); a draft opens with its mail in the inbox. */
function DraftsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const drafts = useQuery(openDraftsQueryOptions);
  const items = useMemo(() => drafts.data ?? [], [drafts.data]);
  const list = useRef<HTMLUListElement>(null);

  const openDraft = useCallback(
    (draft: Draft | undefined) => {
      if (draft?.message_id) void navigate({ to: "/inbox", search: { message: draft.message_id } });
    },
    [navigate],
  );
  const { activeIndex, setActiveIndex } = useListNavigation({
    count: items.length,
    onOpen: (index) => openDraft(items[index]),
  });
  useEffect(() => {
    if (activeIndex < 0) return;
    list.current
      ?.querySelector(`[data-draft-index="${activeIndex}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex]);

  const discard = useMutation({
    mutationFn: (draft: Draft) => discardDraft(draft.id),
    onSuccess: () => {
      toast.success(t("drafts.discarded"));
      void queryClient.invalidateQueries({ queryKey: draftKeys.all });
    },
  });

  const discardMutate = discard.mutate;
  const active = items[activeIndex];
  const commands = useMemo<Command[]>(
    () =>
      active
        ? [
            {
              id: "drafts.discardActive",
              label: t("drafts.discardDraft"),
              group: "actions",
              icon: Trash2,
              run: () => discardMutate(active),
            },
          ]
        : [],
    [active, t, discardMutate],
  );
  useCommands(commands);

  let content: ReactNode;
  if (drafts.isPending) {
    content = <ListSkeleton rows={8} />;
  } else if (drafts.isError) {
    content = (
      <InlineError
        error={drafts.error}
        onRetry={drafts.refetch}
        retrying={drafts.isFetching}
        className="m-4"
      />
    );
  } else if (items.length === 0) {
    content = (
      <EmptyState
        icon={FilePen}
        title={t("drafts.overview.emptyTitle")}
        description={t("drafts.overview.emptyDescription")}
        action={
          <Button asChild size="sm" variant="outline">
            <Link to="/inbox">{t("drafts.overview.emptyAction")}</Link>
          </Button>
        }
      />
    );
  } else {
    content = (
      <ul ref={list} aria-label={t("drafts.overview.listLabel")}>
        {items.map((draft, index) => (
          <li key={draft.id} data-draft-index={index}>
            <DraftRow
              draft={draft}
              active={index === activeIndex}
              onActivate={() => setActiveIndex(index)}
              onDiscard={() => discard.mutate(draft)}
              discarding={discard.isPending && discard.variables?.id === draft.id}
            />
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={t("nav.drafts")}
        meta={items.length > 0 ? new Intl.NumberFormat().format(items.length) : undefined}
      />
      <div className="flex-1 overflow-y-auto pb-8">
        <div className="mx-auto w-full max-w-3xl">{content}</div>
      </div>
    </div>
  );
}

function DraftRow({
  draft,
  active,
  onActivate,
  onDiscard,
  discarding,
}: {
  draft: Draft;
  active: boolean;
  onActivate: () => void;
  onDiscard: () => void;
  discarding: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const recipients = draft.to.map(addressName).join(", ") || t("drafts.noRecipients");
  // Line breaks collapsed: the first line alone is often just the greeting.
  const preview = draft.body.replace(/\s+/g, " ").trim() || t("drafts.overview.noText");
  const inner = (
    <>
      <span className="flex min-w-0 items-baseline gap-3">
        <span className="min-w-0 flex-1 truncate font-medium">{recipients}</span>
        <time
          dateTime={draft.updated_at}
          className="shrink-0 text-xs font-normal text-muted-foreground tabular-nums"
        >
          {formatListDate(draft.updated_at, i18n.language, timezone)}
        </time>
      </span>
      <span className="block truncate text-muted-foreground">
        <span className="text-foreground">{draft.subject || t("mail.noSubject")}</span>
        {" – "}
        {preview}
      </span>
      {!draft.message_id && (
        <span className="block text-xs text-muted-foreground">
          {t("drafts.overview.messageDeleted")}
        </span>
      )}
    </>
  );
  const rowClass = cn(
    "block min-w-0 flex-1 px-4 py-2.5 text-left text-ui outline-none md:px-5",
    "focus-visible:ring-[3px] focus-visible:ring-ring/80 focus-visible:ring-inset",
  );

  return (
    <div
      className={cn(
        "flex items-center border-b border-border/60 pr-2 hover:bg-accent/40",
        active && "bg-accent/60",
      )}
    >
      {draft.message_id ? (
        <Link
          to="/inbox"
          search={{ message: draft.message_id }}
          className={rowClass}
          onFocus={onActivate}
        >
          {inner}
        </Link>
      ) : (
        <div className={rowClass}>{inner}</div>
      )}
      <Button
        size="icon-sm"
        variant="ghost"
        onClick={onDiscard}
        disabled={discarding}
        aria-label={t("drafts.discardNamed", { subject: draft.subject || t("mail.noSubject") })}
        title={t("drafts.discardDraft")}
        className="shrink-0 text-muted-foreground"
      >
        <Trash2 />
      </Button>
    </div>
  );
}
