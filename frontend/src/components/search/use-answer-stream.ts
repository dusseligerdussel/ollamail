import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type AnswerSource,
  type AnswerStatus,
  askQuestion,
  type SearchFilterParams,
  searchKeys,
} from "@/api/search";

export type AnswerPhase = "searching" | "writing" | "done" | "error" | "cancelled";

/** The answer currently being streamed (or the last one, until it is stored). */
export interface LiveAnswer {
  question: string;
  phase: AnswerPhase;
  conversationId?: string;
  answerId?: string;
  sources: AnswerSource[];
  text: string;
  status?: AnswerStatus;
  /** Request error (`ApiError`) or `{ code }` reported by the stream. */
  error?: unknown;
}

export interface AskOptions {
  question: string;
  conversationId?: string;
  filters: SearchFilterParams;
  /** Called with the conversation of the answer as soon as it is known. */
  onConversation?: (conversationId: string) => void;
}

/** Streams answers of "ask your inbox"; one at a time, cancellable. */
export function useAnswerStream() {
  const queryClient = useQueryClient();
  const [answer, setAnswer] = useState<LiveAnswer | undefined>();
  const controller = useRef<AbortController | undefined>(undefined);

  const cancel = useCallback(() => {
    controller.current?.abort();
    controller.current = undefined;
    setAnswer((current) =>
      current && (current.phase === "searching" || current.phase === "writing")
        ? { ...current, phase: "cancelled" }
        : current,
    );
  }, []);

  const reset = useCallback(() => {
    controller.current?.abort();
    controller.current = undefined;
    setAnswer(undefined);
  }, []);

  const ask = useCallback(
    async ({ question, conversationId, filters, onConversation }: AskOptions) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      const update = (change: (current: LiveAnswer) => LiveAnswer) =>
        setAnswer((current) => (current && !abort.signal.aborted ? change(current) : current));
      setAnswer({ question, phase: "searching", conversationId, sources: [], text: "" });

      let finished = false;
      try {
        for await (const event of askQuestion(
          { question, conversation_id: conversationId ?? null, filters },
          abort.signal,
        )) {
          switch (event.type) {
            case "start":
              update((current) => ({
                ...current,
                conversationId: event.conversation_id,
                answerId: event.answer_id,
              }));
              onConversation?.(event.conversation_id);
              break;
            case "sources":
              update((current) => ({ ...current, sources: event.sources }));
              break;
            case "token":
              update((current) => ({
                ...current,
                phase: "writing",
                text: current.text + event.text,
              }));
              break;
            case "done":
              finished = true;
              update((current) => ({ ...current, phase: "done", status: event.status }));
              break;
            case "error":
              finished = true;
              update((current) => ({ ...current, phase: "error", error: { code: event.code } }));
              break;
          }
        }
        if (!finished) {
          update((current) => ({ ...current, phase: "error", error: { code: "interrupted" } }));
        }
      } catch (error) {
        if (abort.signal.aborted) return;
        update((current) => ({ ...current, phase: "error", error }));
      } finally {
        if (controller.current === abort) controller.current = undefined;
        if (!abort.signal.aborted) {
          void queryClient.invalidateQueries({ queryKey: searchKeys.conversations });
        }
      }
    },
    [queryClient],
  );

  // Stop streaming when the page is left.
  useEffect(() => () => controller.current?.abort(), []);

  const streaming = answer?.phase === "searching" || answer?.phase === "writing";
  return { answer, ask, cancel, reset, streaming };
}
