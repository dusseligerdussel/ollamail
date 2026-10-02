import { CircleAlert, Info, Paperclip, Square } from "lucide-react";
import { Fragment, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { describeApiError } from "@/api/errors";
import type { AnswerSource, AnswerStatus } from "@/api/search";
import { KeyHint } from "@/components/key-hint";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import { answerSegments, citedNumbers, parseSourceHeading } from "@/lib/search-text";
import { cn } from "@/lib/utils";

import type { AnswerPhase } from "./use-answer-stream";

export type TurnSource = Pick<
  AnswerSource,
  "number" | "message_id" | "attachment_id" | "heading" | "snippet" | "source"
>;

/** One question with its answer, stored or being streamed. */
export interface AnswerTurn {
  key: string;
  question: string;
  text: string;
  phase: AnswerPhase;
  status?: AnswerStatus | null;
  /** Sources the answer may cite, by number. */
  sources: TurnSource[];
  error?: unknown;
}

interface AnswerViewProps {
  turn: AnswerTurn;
  onOpenSource: (source: TurnSource) => void;
  onCancel?: () => void;
}

const streamErrors = [
  "llm_unavailable",
  "llm_cloud_disabled",
  "llm_error",
  "internal",
  "interrupted",
] as const;
type StreamError = (typeof streamErrors)[number];

function streamErrorCode(error: unknown): StreamError | undefined {
  if (!error || typeof error !== "object" || !("code" in error)) return undefined;
  const code = streamErrors.find((item) => item === error.code);
  return code ?? "internal";
}

function useErrorText(error: unknown): { title: string; description?: string } {
  const { t } = useTranslation();
  const code = streamErrorCode(error);
  if (code) return { title: t(`search.answer.errors.${code}`) };
  return describeApiError(error, t);
}

function AnswerError({ error }: { error: unknown }) {
  const { title, description } = useErrorText(error);
  return (
    <div role="alert" className="flex items-start gap-2 text-ui">
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0">
        <p className="text-destructive">{title}</p>
        {description && <p className="text-muted-foreground">{description}</p>}
      </div>
    </div>
  );
}

/** A question, the answer text with citation markers and the numbered sources. */
export function AnswerView({ turn, onOpenSource, onCancel }: AnswerViewProps) {
  const { t } = useTranslation();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const byNumber = useMemo(
    () => new Map(turn.sources.map((source) => [source.number, source])),
    [turn.sources],
  );
  const segments = useMemo(() => answerSegments(turn.text), [turn.text]);
  const cited = useMemo(
    () =>
      citedNumbers(turn.text)
        .map((number) => byNumber.get(number))
        .filter((source): source is TurnSource => source !== undefined),
    [turn.text, byNumber],
  );
  const streaming = turn.phase === "searching" || turn.phase === "writing";
  const noEvidence = turn.phase === "done" && turn.status === "no_evidence";

  return (
    <article aria-label={turn.question} className="flex flex-col gap-3 px-4 py-4 md:px-5">
      <div className="flex items-start gap-3">
        <h2 className="min-w-0 flex-1 text-sm font-medium [overflow-wrap:anywhere]">
          {turn.question}
        </h2>
        {streaming && onCancel && (
          <Button variant="outline" size="xs" onClick={onCancel} className="shrink-0">
            <Square aria-hidden className="size-3" />
            {t("search.answer.cancel")}
            {hasKeyboard && <KeyHint keys="escape" className="ml-1" />}
          </Button>
        )}
      </div>

      {turn.phase === "searching" && (
        <div role="status" aria-busy="true" className="flex flex-col gap-2">
          <span className="text-ui text-muted-foreground">{t("search.answer.searching")}</span>
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-5/6" />
          <Skeleton className="h-3 w-2/3" />
        </div>
      )}

      {noEvidence && (
        <div className="flex items-start gap-2 rounded-md border bg-muted/50 px-3 py-2 text-ui">
          <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <p className="font-medium">{t("search.answer.noEvidenceTitle")}</p>
            <p className="text-muted-foreground">{t("search.answer.noEvidenceDescription")}</p>
          </div>
        </div>
      )}

      {turn.text && (
        <div
          aria-live={streaming ? "polite" : undefined}
          aria-busy={streaming || undefined}
          className={cn(
            "text-sm leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]",
            noEvidence && "text-muted-foreground",
          )}
        >
          {segments.map((segment, index) => {
            if ("text" in segment) {
              // biome-ignore lint/suspicious/noArrayIndexKey: segments have no identity besides position
              return <Fragment key={index}>{segment.text}</Fragment>;
            }
            const source = byNumber.get(segment.citation);
            if (!source) {
              // biome-ignore lint/suspicious/noArrayIndexKey: segments have no identity besides position
              return <Fragment key={index}>{`[${segment.citation}]`}</Fragment>;
            }
            return (
              <button
                // biome-ignore lint/suspicious/noArrayIndexKey: a source may be cited several times
                key={index}
                type="button"
                onClick={() => onOpenSource(source)}
                aria-label={t("search.answer.openSource", { number: source.number })}
                className="mx-0.5 inline-flex h-4 min-w-4 -translate-y-px items-center justify-center rounded-sm border px-1 align-middle text-[11px] leading-none font-medium text-muted-foreground tabular-nums outline-none hover:border-brand hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
              >
                {source.number}
              </button>
            );
          })}
        </div>
      )}

      {turn.phase === "cancelled" && (
        <p className="text-ui text-muted-foreground">{t("search.answer.cancelled")}</p>
      )}
      {turn.phase === "error" && <AnswerError error={turn.error} />}

      {cited.length > 0 && !noEvidence && (
        <section aria-label={t("search.answer.sources")} className="flex flex-col gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">
            {t("search.answer.sources")}
          </h3>
          <ol className="flex flex-col">
            {cited.map((source) => (
              <SourceItem key={source.number} source={source} onOpen={onOpenSource} />
            ))}
          </ol>
        </section>
      )}
    </article>
  );
}

function SourceItem({
  source,
  onOpen,
}: {
  source: TurnSource;
  onOpen: (source: TurnSource) => void;
}) {
  const { t, i18n } = useTranslation();
  const heading = parseSourceHeading(source.heading);
  const date =
    heading.date && !Number.isNaN(Date.parse(heading.date))
      ? new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeZone: "UTC" }).format(
          new Date(heading.date),
        )
      : heading.date;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(source)}
        className="-mx-2 flex w-[calc(100%+1rem)] items-start gap-2.5 rounded-md px-2 py-1.5 text-left text-ui outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        <span className="mt-px flex h-4 min-w-4 shrink-0 items-center justify-center rounded-sm border px-1 text-[11px] leading-none font-medium text-muted-foreground tabular-nums">
          {source.number}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline gap-2">
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium">{heading.sender ?? t("mail.unknownSender")}</span>
              {heading.subject && (
                <span className="text-muted-foreground"> · {heading.subject}</span>
              )}
            </span>
            {date && (
              <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{date}</span>
            )}
          </span>
          {source.source === "attachment" && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Paperclip aria-hidden className="size-3 shrink-0" />
              <span className="truncate">{heading.attachment || t("mail.unnamedAttachment")}</span>
            </span>
          )}
          <span className="line-clamp-2 text-muted-foreground [overflow-wrap:anywhere]">
            {source.snippet}
          </span>
        </span>
      </button>
    </li>
  );
}
