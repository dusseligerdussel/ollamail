import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Newspaper, Pause, Play, RefreshCw, Settings2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import {
  type Digest,
  type DigestSummary,
  digestQueryOptions,
  digestSettingsQueryOptions,
  digestsQueryOptions,
  isInProgress,
  useCreateDigest,
} from "@/api/digest";
import { useCommands } from "@/components/command-palette/command-provider";
import { AudioPlayer } from "@/components/digest/audio-player";
import {
  DigestPlayerProvider,
  formatDuration,
  playbackRates,
  SKIP_SECONDS,
  useDigestPlayer,
} from "@/components/digest/player-provider";
import { Transcript } from "@/components/digest/transcript";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { KeyHint } from "@/components/key-hint";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { SplitView } from "@/components/split-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentUser } from "@/hooks/use-current-user";
import { useListNavigation } from "@/hooks/use-list-navigation";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import type { Command } from "@/lib/commands";
import { formatDateTime, formatListDate } from "@/lib/mail-format";
import { cn } from "@/lib/utils";

interface DigestSearch {
  /** The opened digest; without it the newest one is shown on wide screens. */
  digest?: string;
}

const ID = /^[0-9a-f-]{36}$/i;

export const Route = createFileRoute("/digest")({
  validateSearch: (search: Record<string, unknown>): DigestSearch =>
    typeof search.digest === "string" && ID.test(search.digest) ? { digest: search.digest } : {},
  component: DigestRoute,
});

function DigestRoute() {
  return (
    <DigestPlayerProvider>
      <DigestPage />
    </DigestPlayerProvider>
  );
}

function DigestPage() {
  const { t } = useTranslation();
  const search = Route.useSearch();
  const navigate = useNavigate({ from: "/digest" });
  const split = useMediaQuery(mediaQueries.split);
  const player = useDigestPlayer();

  const digests = useInfiniteQuery(digestsQueryOptions);
  const items = useMemo(() => digests.data?.pages.flat() ?? [], [digests.data]);
  const newest = items[0];
  const selectedId = search.digest ?? (split ? newest?.id : undefined);
  const create = useCreateDigest();
  const busy = create.isPending || items.some(isInProgress);

  const open = useCallback(
    (digestId: string) => void navigate({ search: { digest: digestId } }),
    [navigate],
  );
  const close = useCallback(() => void navigate({ search: {} }), [navigate]);
  const generate = useCallback(
    () => create.mutate(undefined, { onSuccess: (digest) => open(digest.id) }),
    [create, open],
  );

  const { activeIndex, setActiveIndex } = useListNavigation({
    count: items.length,
    onOpen: (index) => {
      const digest = items[index];
      if (digest) open(digest.id);
    },
  });
  useEffect(() => {
    if (!selectedId) return;
    const index = items.findIndex((digest) => digest.id === selectedId);
    if (index >= 0) setActiveIndex(index);
  }, [selectedId, items, setActiveIndex]);

  useShortcut(
    {
      id: "digest.close",
      keys: "escape",
      group: "list",
      description: t("digest.shortcuts.close"),
      enabled: !!search.digest && !split,
    },
    close,
  );

  const commands = useMemo<Command[]>(
    () => [
      {
        id: "digest.generate",
        label: t("digest.generateNow"),
        group: "actions",
        icon: RefreshCw,
        run: generate,
      },
      {
        id: "digest.settings",
        label: t("digest.settings.title"),
        group: "actions",
        icon: Settings2,
        run: () => void navigate({ to: "/digest/settings" }),
      },
    ],
    [t, generate, navigate],
  );
  useCommands(commands);

  let listContent: ReactNode;
  if (digests.isPending) {
    listContent = <ListSkeleton rows={8} />;
  } else if (digests.isError) {
    listContent = <InlineError error={digests.error} className="m-4" />;
  } else if (items.length === 0) {
    listContent = <NoDigests onGenerate={generate} busy={busy} />;
  } else {
    listContent = (
      <DigestList
        items={items}
        selectedId={selectedId}
        activeIndex={activeIndex}
        hasNextPage={digests.hasNextPage}
        isFetchingNextPage={digests.isFetchingNextPage}
        fetchNextPage={() => void digests.fetchNextPage()}
      />
    );
  }

  const detailVisible = split || !!search.digest;
  const showMiniPlayer =
    !!player.loaded && !(detailVisible && selectedId === player.loaded.id) && items.length > 0;

  return (
    <SplitView
      id="digest"
      detailOpen={!!search.digest}
      list={
        <>
          <PageHeader
            title={t("nav.digest")}
            actions={
              <>
                {/* The empty state offers the same action. */}
                {items.length > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-ui"
                    disabled={busy}
                    onClick={generate}
                  >
                    <RefreshCw aria-hidden="true" />
                    {t("digest.generateNow")}
                  </Button>
                )}
                <Button asChild variant="ghost" size="icon-sm">
                  <Link
                    to="/digest/settings"
                    aria-label={t("digest.settings.title")}
                    title={t("digest.settings.title")}
                  >
                    <Settings2 />
                  </Link>
                </Button>
              </>
            }
          />
          {listContent}
          {showMiniPlayer && <MiniPlayer items={items} onOpen={open} />}
        </>
      }
      detail={
        selectedId ? (
          <DigestDetail
            key={selectedId}
            digestId={selectedId}
            onBack={split ? undefined : close}
            onRetry={generate}
            retryDisabled={busy}
          />
        ) : (
          <>
            <div aria-hidden="true" className="h-header shrink-0 border-b" />
            <EmptyState
              title={t("pages.digest.noSelection")}
              description={
                items.length > 0 ? (
                  <Trans
                    i18nKey="common.navigateHint"
                    components={[<KeyHint key="j" keys="j" />, <KeyHint key="k" keys="k" />]}
                  />
                ) : undefined
              }
            />
          </>
        )
      }
    />
  );
}

function NoDigests({ onGenerate, busy }: { onGenerate: () => void; busy: boolean }) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const settings = useQuery(digestSettingsQueryOptions);
  const next = settings.data?.next_run_at;
  return (
    <EmptyState
      icon={Newspaper}
      title={t("pages.digest.emptyTitle")}
      description={
        next
          ? t("digest.firstScheduled", { date: formatDateTime(next, i18n.language, timezone) })
          : t("pages.digest.emptyDescription")
      }
      action={
        <div className="flex flex-wrap justify-center gap-2">
          <Button size="sm" disabled={busy} onClick={onGenerate}>
            {t("digest.generateNow")}
          </Button>
          <Button asChild size="sm" variant="outline">
            <Link to="/digest/settings">{t("pages.digest.emptyAction")}</Link>
          </Button>
        </div>
      }
    />
  );
}

function DigestList({
  items,
  selectedId,
  activeIndex,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
}: {
  items: DigestSummary[];
  selectedId?: string;
  activeIndex: number;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
}) {
  const { t } = useTranslation();
  const [current, ...archive] = items;
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      {current && (
        <section aria-labelledby="digest-current">
          <h2
            id="digest-current"
            className="px-4 pt-4 pb-1.5 text-xs font-medium text-muted-foreground"
          >
            {t("digest.current")}
          </h2>
          <ul className="border-t border-border/60">
            <DigestRow
              digest={current}
              selected={current.id === selectedId}
              active={activeIndex === 0}
            />
          </ul>
        </section>
      )}
      {archive.length > 0 && (
        <section aria-labelledby="digest-archive">
          <h2
            id="digest-archive"
            className="px-4 pt-5 pb-1.5 text-xs font-medium text-muted-foreground"
          >
            {t("digest.archive")}
          </h2>
          <ul className="border-t border-border/60">
            {archive.map((digest, index) => (
              <DigestRow
                key={digest.id}
                digest={digest}
                selected={digest.id === selectedId}
                active={activeIndex === index + 1}
              />
            ))}
          </ul>
        </section>
      )}
      {hasNextPage && (
        <div className="flex justify-center p-3">
          <Button
            variant="ghost"
            size="sm"
            className="text-ui"
            disabled={isFetchingNextPage}
            onClick={fetchNextPage}
          >
            {t("digest.loadOlder")}
          </Button>
        </div>
      )}
    </div>
  );
}

function DigestMeta({ digest }: { digest: DigestSummary }) {
  const { t } = useTranslation();
  if (digest.status === "failed") {
    return <span className="text-destructive">{t("digest.status.failed")}</span>;
  }
  if (isInProgress(digest)) {
    return <span>{t(`digest.status.${digest.status}`)}</span>;
  }
  const parts = [t("digest.mailCount", { count: digest.message_count })];
  if (digest.todo_count > 0) parts.push(t("digest.todoCount", { count: digest.todo_count }));
  if (digest.duration_seconds) parts.push(formatDuration(digest.duration_seconds));
  return <span>{parts.join(" · ")}</span>;
}

function DigestRow({
  digest,
  selected,
  active,
}: {
  digest: DigestSummary;
  selected: boolean;
  active: boolean;
}) {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const date = formatListDate(digest.generated_at ?? digest.created_at, i18n.language, timezone);
  return (
    <li>
      <Link
        to="/digest"
        search={{ digest: digest.id }}
        aria-current={selected ? "true" : undefined}
        data-active={active || undefined}
        className={cn(
          "flex flex-col justify-center gap-0.5 border-b border-border/60 px-4 py-2.5 text-ui outline-none",
          "hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/80 focus-visible:ring-inset",
          "data-active:shadow-[inset_2px_0_0_var(--ring)]",
          selected && "bg-accent hover:bg-accent",
        )}
      >
        <span className="flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate font-medium">{digest.title}</span>
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{date}</span>
        </span>
        <span className="truncate text-muted-foreground">
          <DigestMeta digest={digest} />
        </span>
      </Link>
    </li>
  );
}

/** Shown below the list while a digest plays that is not shown in the detail column. */
function MiniPlayer({
  items,
  onOpen,
}: {
  items: DigestSummary[];
  onOpen: (digestId: string) => void;
}) {
  const { t } = useTranslation();
  const player = useDigestPlayer();
  const loaded = player.loaded;
  const digest = items.find((item) => item.id === loaded?.id);
  if (!loaded) return null;
  return (
    <div className="flex shrink-0 items-center gap-2 border-t bg-background px-2 py-2">
      <Button
        variant="ghost"
        size="icon-sm"
        disabled={!digest}
        onClick={() => digest && player.toggle(digest)}
        aria-label={player.playing ? t("digest.player.pause") : t("digest.player.play")}
      >
        {player.playing ? <Pause /> : <Play />}
      </Button>
      <button
        type="button"
        onClick={() => onOpen(loaded.id)}
        className="flex min-w-0 flex-1 flex-col items-start rounded-md px-1 text-left text-ui outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80"
      >
        <span className="w-full truncate font-medium">{loaded.title}</span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {formatDuration(player.currentTime)} / {formatDuration(player.duration)}
        </span>
      </button>
    </div>
  );
}

function DigestDetail({
  digestId,
  onBack,
  onRetry,
  retryDisabled,
}: {
  digestId: string;
  onBack?: () => void;
  onRetry: () => void;
  retryDisabled: boolean;
}) {
  const { t } = useTranslation();
  const digest = useQuery(digestQueryOptions(digestId));
  const back = onBack && (
    <Button variant="ghost" size="icon-sm" onClick={onBack} aria-label={t("digest.back")}>
      <ArrowLeft />
    </Button>
  );

  if (digest.isPending) {
    return (
      <>
        <PageHeader title={t("nav.digest")} leading={back} />
        <DetailSkeleton />
      </>
    );
  }
  if (digest.isError) {
    return (
      <>
        <PageHeader title={t("nav.digest")} leading={back} />
        <InlineError error={digest.error} className="m-4" />
      </>
    );
  }
  return (
    <>
      <PageHeader title={digest.data.title} leading={back} />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-5 md:px-6">
          <DigestBody digest={digest.data} onRetry={onRetry} retryDisabled={retryDisabled} />
        </div>
      </div>
    </>
  );
}

function DigestBody({
  digest,
  onRetry,
  retryDisabled,
}: {
  digest: Digest;
  onRetry: () => void;
  retryDisabled: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const date = formatDateTime(digest.generated_at ?? digest.created_at, i18n.language, timezone);
  const playable = digest.status === "ready" && digest.audio_formats.length > 0;

  return (
    <>
      <p className="text-ui text-muted-foreground">
        {date}
        {digest.status === "ready" && (
          <>
            {" · "}
            <DigestMeta digest={digest} />
          </>
        )}
      </p>
      {playable && (
        <>
          <PlayerShortcuts digest={digest} />
          <AudioPlayer digest={digest} />
        </>
      )}
      {isInProgress(digest) && (
        <div role="status" className="flex flex-col gap-3">
          <p className="text-ui">{t(`digest.status.${digest.status}`)}</p>
          <p className="text-ui text-muted-foreground">{t("digest.inProgressHint")}</p>
          <div aria-hidden="true" className="flex flex-col gap-2 pt-2">
            <Skeleton className="h-3 w-11/12" />
            <Skeleton className="h-3 w-4/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
        </div>
      )}
      {digest.status === "failed" && (
        <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border p-4">
          <div>
            <p className="text-ui font-medium text-destructive">{t("digest.status.failed")}</p>
            <p className="text-ui text-muted-foreground">
              {t(`digest.errors.${digest.error_code ?? "digest_failed"}`, {
                defaultValue: t("digest.errors.digest_failed"),
              })}
            </p>
          </div>
          <Button size="sm" variant="outline" disabled={retryDisabled} onClick={onRetry}>
            {t("digest.retry")}
          </Button>
        </div>
      )}
      {digest.script && <Transcript script={digest.script} references={digest.references} />}
    </>
  );
}

/** Space plays/pauses, arrow keys skip, `<`/`>` change the speed. */
const ACTIVATABLE =
  'button, a[href], summary, [role="button"], [role="link"], [role="checkbox"], [role="switch"], [role="menuitem"], [role="tab"]';

/** Whether focus is on a control that Space activates itself (then Space is left to it). */
function useFocusOnControl() {
  const [onControl, setOnControl] = useState(false);
  useEffect(() => {
    const update = () => {
      const active = document.activeElement;
      setOnControl(active instanceof Element && active.matches(ACTIVATABLE));
    };
    update();
    document.addEventListener("focusin", update);
    document.addEventListener("focusout", update);
    return () => {
      document.removeEventListener("focusin", update);
      document.removeEventListener("focusout", update);
    };
  }, []);
  return onControl;
}

function PlayerShortcuts({ digest }: { digest: DigestSummary }) {
  const { t } = useTranslation();
  const player = useDigestPlayer();
  const focusOnControl = useFocusOnControl();
  const active = player.loaded?.id === digest.id;
  const changeRate = (step: number) => {
    const index = (playbackRates as readonly number[]).indexOf(player.rate);
    const next = playbackRates[Math.min(Math.max(index + step, 0), playbackRates.length - 1)];
    if (next !== undefined) player.setRate(next);
  };
  useShortcut(
    {
      id: "digest.playPause",
      keys: "space",
      group: "general",
      description: t("digest.shortcuts.playPause"),
      enabled: !focusOnControl,
    },
    () => player.toggle(digest),
  );
  useShortcut(
    {
      id: "digest.back",
      keys: "left",
      group: "general",
      description: t("digest.player.back", { seconds: SKIP_SECONDS }),
      enabled: active,
    },
    () => player.seekBy(-SKIP_SECONDS),
  );
  useShortcut(
    {
      id: "digest.forward",
      keys: "right",
      group: "general",
      description: t("digest.player.forward", { seconds: SKIP_SECONDS }),
      enabled: active,
    },
    () => player.seekBy(SKIP_SECONDS),
  );
  useShortcut(
    { id: "digest.slower", keys: "<", group: "general", description: t("digest.shortcuts.slower") },
    () => changeRate(-1),
  );
  useShortcut(
    { id: "digest.faster", keys: ">", group: "general", description: t("digest.shortcuts.faster") },
    () => changeRate(1),
  );
  return null;
}

function DetailSkeleton() {
  const { t } = useTranslation();
  return (
    <div
      role="status"
      aria-busy="true"
      className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-5 md:px-6"
    >
      <span className="sr-only">{t("common.loading")}</span>
      <Skeleton className="h-3 w-48" />
      <Skeleton className="h-14 w-full rounded-lg" />
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-4/5" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-2/3" />
      </div>
    </div>
  );
}
