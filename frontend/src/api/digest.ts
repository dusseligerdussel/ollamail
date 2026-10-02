import {
  infiniteQueryOptions,
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";

import { API_BASE_PATH, api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type DigestSummary = components["schemas"]["DigestSummary"];
export type Digest = components["schemas"]["DigestRead"];
export type DigestReference = components["schemas"]["DigestReference"];
export type DigestStatus = components["schemas"]["DigestStatus"];
export type DigestSettings = components["schemas"]["DigestSettingsRead"];
export type DigestSettingsUpdate = components["schemas"]["DigestSettingsUpdate"];
export type DigestVoice = components["schemas"]["DigestVoice"];
export type DigestLength = components["schemas"]["DigestLength"];
export type AudioFormat = components["schemas"]["DigestSummary"]["audio_formats"][number];
export type FeedStatus = components["schemas"]["FeedStatus"];
export type FeedCreated = components["schemas"]["FeedCreated"];

/**
 * Query keys. The server event `digest.changed` invalidates everything under `["digest"]`
 * (default rule in `use-events.ts`).
 */
export const digestKeys = {
  all: ["digest"] as const,
  list: ["digest", "list"] as const,
  detail: (digestId: string) => ["digest", "detail", digestId] as const,
  settings: ["digest", "settings"] as const,
  voices: ["digest", "voices"] as const,
};

const IN_PROGRESS: ReadonlySet<DigestStatus> = new Set(["pending", "summarizing", "synthesizing"]);

export function isInProgress(digest: Pick<DigestSummary, "status">) {
  return IN_PROGRESS.has(digest.status);
}

// While a digest is being generated, poll as a fallback for missed server events.
const POLL_MS = 5000;

export const DIGEST_PAGE_SIZE = 30;

export const digestsQueryOptions = infiniteQueryOptions({
  queryKey: digestKeys.list,
  queryFn: ({ pageParam, signal }) =>
    unwrap(
      api.GET("/digests", {
        params: { query: { limit: DIGEST_PAGE_SIZE, offset: pageParam } },
        signal,
      }),
    ),
  initialPageParam: 0,
  getNextPageParam: (lastPage, pages) =>
    lastPage.length < DIGEST_PAGE_SIZE ? undefined : pages.length * DIGEST_PAGE_SIZE,
  refetchInterval: (query) =>
    query.state.data?.pages.some((page) => page.some(isInProgress)) ? POLL_MS : false,
});

export function digestQueryOptions(digestId: string) {
  return queryOptions({
    queryKey: digestKeys.detail(digestId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/digests/{digest_id}", { params: { path: { digest_id: digestId } }, signal }),
      ),
    refetchInterval: (query) =>
      query.state.data && isInProgress(query.state.data) ? POLL_MS : false,
  });
}

export const digestSettingsQueryOptions = queryOptions({
  queryKey: digestKeys.settings,
  queryFn: ({ signal }) => unwrap(api.GET("/digests/settings", { signal })),
});

export const digestVoicesQueryOptions = queryOptions({
  queryKey: digestKeys.voices,
  queryFn: ({ signal }) => unwrap(api.GET("/digests/voices", { signal })),
  staleTime: 5 * 60_000,
});

/** Audio of a digest for the web player (same origin, the session cookie authenticates). */
export function audioUrl(digestId: string, format: AudioFormat) {
  return `${API_BASE_PATH}/digests/${encodeURIComponent(digestId)}/audio.${format}`;
}

/** Opus if the browser plays it (smaller), otherwise MP3. */
export function pickAudioFormat(
  formats: readonly AudioFormat[],
  canPlay: (type: string) => boolean,
): AudioFormat | undefined {
  if (formats.includes("opus") && canPlay('audio/ogg; codecs="opus"')) return "opus";
  if (formats.includes("mp3") && canPlay("audio/mpeg")) return "mp3";
  return formats[0];
}

export function useCreateDigest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/digests")),
    onSuccess: (digest) => {
      queryClient.setQueryData(digestKeys.detail(digest.id), digest);
      void queryClient.invalidateQueries({ queryKey: digestKeys.list });
      void queryClient.invalidateQueries({ queryKey: digestKeys.settings });
    },
  });
}

export function useUpdateDigestSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DigestSettingsUpdate) => unwrap(api.PATCH("/digests/settings", { body })),
    onSuccess: (settings) => queryClient.setQueryData(digestKeys.settings, settings),
  });
}

function useSetFeedStatus() {
  const queryClient = useQueryClient();
  return (feed: FeedStatus) =>
    queryClient.setQueryData(digestKeys.settings, (settings) => settings && { ...settings, feed });
}

/** Creates a new feed URL; a previous one stops working. The URL is only returned here. */
export function useCreateFeed() {
  const setFeed = useSetFeedStatus();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/digests/feed")),
    onSuccess: (created) => setFeed({ active: true, created_at: created.created_at }),
  });
}

export function useRevokeFeed() {
  const setFeed = useSetFeedStatus();
  return useMutation({
    mutationFn: () => unwrap(api.DELETE("/digests/feed")),
    onSuccess: () => setFeed({ active: false, created_at: null }),
  });
}
