import { useMutation } from "@tanstack/react-query";
import { CircleAlert, PenLine, Send, Trash2 } from "lucide-react";
import {
  type KeyboardEvent,
  type Ref,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import {
  type Draft,
  deleteDraft,
  discardDraft,
  generateDraft,
  sendDraft,
  updateDraft,
} from "@/api/drafts";
import { describeApiError } from "@/api/errors";
import { useCommands } from "@/components/command-palette/command-provider";
import { KeyHint } from "@/components/key-hint";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import type { Command } from "@/lib/commands";
import { addressFull } from "@/lib/mail-format";
import {
  forgetSuggestion,
  generateErrorCode,
  isUnchangedSuggestion,
  rememberSuggestion,
  sendErrorKey,
  suggestionOf,
} from "@/lib/reply-draft";
import { cn } from "@/lib/utils";

/** Delay between the last keystroke and saving the text. */
export const AUTOSAVE_MS = 800;

export interface ReplyEditorHandle {
  focus: () => void;
  /** Shows the field for a short instruction, then generates the text. */
  suggest: () => void;
}

interface ReplyEditorProps {
  draft: Draft;
  /** The answered mail (the draft keeps its ID, but it is `null` once the mail is deleted). */
  messageId: string;
  /** The draft changed on the server (recipients, generated text). */
  onDraftChange: (draft: Draft) => void;
  onReplyAllChange: (replyAll: boolean) => void;
  /** The draft was sent or discarded; the editor is closed. */
  onDone: (outcome: "sent" | "discarded") => void;
  ref?: Ref<ReplyEditorHandle>;
}

type SaveState = "saved" | "saving" | "error";

/**
 * An empty draft is deleted when its editor goes away (e.g. `r` and then the next mail).
 * Deferred, so React's development remount (StrictMode) does not delete a draft in use.
 */
const pendingDeletes = new Map<string, ReturnType<typeof setTimeout>>();

/**
 * Plain-text reply below the thread (#93). The text is saved automatically; nothing is
 * sent without "Send", and an unchanged suggestion needs one more confirmation.
 */
export function ReplyEditor({
  draft,
  messageId,
  onDraftChange,
  onReplyAllChange,
  onDone,
  ref,
}: ReplyEditorProps) {
  const { t } = useTranslation();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const ids = useId();
  const root = useRef<HTMLElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const instructionInput = useRef<HTMLInputElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);

  const [body, setBody] = useState(draft.body);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  // The stored text, for rendering (`saved` below is read in callbacks).
  const [savedText, setSavedText] = useState(draft.body);
  const [suggestion, setSuggestion] = useState(() => suggestionOf(draft.id));
  const [instructionOpen, setInstructionOpen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [writing, setWriting] = useState(false);
  const [generateError, setGenerateError] = useState<unknown>();
  const [confirming, setConfirming] = useState(false);

  const bodyRef = useRef(body);
  bodyRef.current = body;
  const saved = useRef(draft.body);
  const closed = useRef(false);
  const writingRef = useRef(false);
  writingRef.current = writing;
  const before = useRef("");
  const controller = useRef<AbortController | undefined>(undefined);
  const chain = useRef<Promise<unknown>>(Promise.resolve());

  // -- saving --------------------------------------------------------------------------

  /** Saves the current text after any save still running; rejects if saving fails. */
  const save = useCallback(() => {
    const run = chain.current.then(async () => {
      const text = bodyRef.current;
      if (closed.current || text === saved.current) return;
      setSaveState("saving");
      try {
        await updateDraft(draft.id, { body: text });
      } catch (error) {
        setSaveState("error");
        throw error;
      }
      saved.current = text;
      setSavedText(text);
      setSaveState("saved");
    });
    chain.current = run.catch(() => undefined);
    return run;
  }, [draft.id]);

  useEffect(() => {
    if (writing || body === saved.current) return;
    const timer = setTimeout(() => void save().catch(() => undefined), AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [body, writing, save]);

  // Leaving the thread: save what is left, delete a draft that never got any text.
  useEffect(() => {
    const id = draft.id;
    clearTimeout(pendingDeletes.get(id));
    pendingDeletes.delete(id);
    return () => {
      controller.current?.abort();
      if (closed.current) return;
      if (bodyRef.current.trim() === "" && saved.current.trim() === "") {
        pendingDeletes.set(
          id,
          setTimeout(() => {
            pendingDeletes.delete(id);
            void deleteDraft(id).catch(() => undefined);
          }, 0),
        );
      } else if (!writingRef.current && bodyRef.current !== saved.current) {
        void save().catch(() => undefined);
      }
    };
  }, [draft.id, save]);

  // -- suggestion ----------------------------------------------------------------------

  const generate = useCallback(
    async (text: string) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      before.current = bodyRef.current;
      setInstructionOpen(false);
      setGenerateError(undefined);
      setConfirming(false);
      setWriting(true);
      setBody("");
      const restore = (error: unknown) => {
        setBody(before.current);
        setGenerateError(error);
      };

      let finished = false;
      try {
        for await (const event of generateDraft(
          {
            message_id: draft.message_id ?? messageId,
            instruction: text.trim() || null,
            reply_all: draft.reply_all,
            draft_id: draft.id,
          },
          abort.signal,
        )) {
          if (abort.signal.aborted) return;
          if (event.type === "token") {
            setBody((current) => current + event.text);
          } else if (event.type === "done") {
            finished = true;
            saved.current = event.draft.body;
            setSavedText(event.draft.body);
            setBody(event.draft.body);
            setSaveState("saved");
            rememberSuggestion(draft.id, event.draft.body);
            setSuggestion(event.draft.body);
            onDraftChange(event.draft);
          } else if (event.type === "error") {
            finished = true;
            restore({ code: event.code });
          }
        }
        if (!finished && !abort.signal.aborted) restore({ code: "interrupted" });
      } catch (error) {
        if (!abort.signal.aborted) restore(error);
      } finally {
        if (controller.current === abort) {
          controller.current = undefined;
          setWriting(false);
          textarea.current?.focus();
        }
      }
    },
    [draft.id, draft.message_id, draft.reply_all, messageId, onDraftChange],
  );

  /** Stops the generation; the server stores nothing, so the previous text comes back. */
  const cancel = useCallback(() => {
    if (!controller.current) return;
    controller.current.abort();
    controller.current = undefined;
    setBody(before.current);
    setWriting(false);
    textarea.current?.focus();
  }, []);

  const openInstruction = useCallback(() => {
    if (writingRef.current) return;
    setConfirming(false);
    setInstructionOpen(true);
    // The row renders first.
    requestAnimationFrame(() => instructionInput.current?.focus());
  }, []);

  // -- send and discard -------------------------------------------------------------------

  const send = useMutation({
    mutationFn: async () => {
      await save();
      return sendDraft(draft.id);
    },
    meta: { errorToast: false },
    onSuccess: () => {
      closed.current = true;
      forgetSuggestion(draft.id);
      onDone("sent");
    },
  });
  const discard = useMutation({
    mutationFn: () => discardDraft(draft.id),
    onSuccess: () => {
      closed.current = true;
      forgetSuggestion(draft.id);
      onDone("discarded");
    },
  });

  const discardDraftNow = discard.mutate;
  const startSend = send.mutate;
  const empty = body.trim() === "";
  const busy = writing || send.isPending || discard.isPending;
  const requestSend = useCallback(() => {
    if (busy || empty) return;
    if (isUnchangedSuggestion(bodyRef.current, suggestion)) {
      setInstructionOpen(false);
      setConfirming(true);
      requestAnimationFrame(() => confirmButton.current?.focus());
      return;
    }
    startSend();
  }, [busy, empty, suggestion, startSend]);
  const confirmSend = () => {
    setConfirming(false);
    send.mutate();
  };
  const keepEditing = () => {
    setConfirming(false);
    textarea.current?.focus();
  };

  useImperativeHandle(
    ref,
    () => ({
      focus: () => {
        root.current?.scrollIntoView?.({ block: "nearest" });
        textarea.current?.focus();
      },
      suggest: () => {
        root.current?.scrollIntoView?.({ block: "nearest" });
        openInstruction();
      },
    }),
    [openInstruction],
  );

  // Fields of the editor handle ⌘Enter / Esc themselves; this covers the rest of the page.
  useShortcut(
    {
      id: "drafts.send",
      keys: "mod+enter",
      group: "list",
      description: t("drafts.shortcuts.send"),
    },
    requestSend,
  );

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (confirming) confirmSend();
      else requestSend();
    } else if (event.key === "Escape") {
      if (writing) {
        event.preventDefault();
        cancel();
      } else if (instructionOpen) {
        event.preventDefault();
        setInstructionOpen(false);
        textarea.current?.focus();
      } else if (confirming) {
        event.preventDefault();
        keepEditing();
      }
    }
  };

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [];
    if (writing) {
      list.push({
        id: "drafts.cancel",
        label: t("drafts.cancel"),
        group: "actions",
        icon: PenLine,
        run: cancel,
      });
      return list;
    }
    if (!empty) {
      list.push({
        id: "drafts.send",
        label: t("drafts.shortcuts.send"),
        group: "actions",
        icon: Send,
        shortcut: "mod+enter",
        run: requestSend,
      });
    }
    list.push(
      {
        id: "drafts.suggest",
        label: t("drafts.suggest"),
        group: "actions",
        icon: PenLine,
        run: openInstruction,
      },
      {
        id: "drafts.discard",
        label: t("drafts.discardDraft"),
        group: "actions",
        icon: Trash2,
        run: () => discardDraftNow(),
      },
    );
    return list;
  }, [t, writing, empty, cancel, requestSend, openInstruction, discardDraftNow]);
  useCommands(commands);

  const recipients = draft.to.map(addressFull).join(", ");
  const copies = draft.cc.map(addressFull).join(", ");
  const sendError = send.error;

  return (
    <section
      ref={root}
      aria-labelledby={`${ids}-title`}
      className="rounded-lg border"
      onKeyDown={onKeyDown}
    >
      <header className="flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-border/60 px-4 py-2.5">
        <div className="min-w-0 flex-1 text-ui">
          <h2 id={`${ids}-title`} className="sr-only">
            {t("drafts.editorLabel")}
          </h2>
          <p className="truncate">
            <span className="text-muted-foreground">{t("drafts.to")} </span>
            {recipients || (
              <span className="text-muted-foreground">{t("drafts.noRecipients")}</span>
            )}
          </p>
          {copies && (
            <p className="truncate">
              <span className="text-muted-foreground">{t("drafts.cc")} </span>
              {copies}
            </p>
          )}
        </div>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={draft.reply_all ? "all" : "sender"}
          onValueChange={(value) => value && onReplyAllChange(value === "all")}
          disabled={busy}
          aria-label={t("drafts.recipientsLabel")}
          className="shrink-0"
        >
          <ToggleGroupItem
            value="sender"
            className="px-2.5 text-ui data-[state=off]:text-muted-foreground"
          >
            {t("drafts.reply")}
          </ToggleGroupItem>
          <ToggleGroupItem
            value="all"
            className="px-2.5 text-ui data-[state=off]:text-muted-foreground"
          >
            {t("drafts.replyAll")}
          </ToggleGroupItem>
        </ToggleGroup>
      </header>

      <label htmlFor={`${ids}-body`} className="sr-only">
        {t("drafts.bodyLabel")}
      </label>
      <textarea
        ref={textarea}
        id={`${ids}-body`}
        value={body}
        onChange={(event) => {
          setBody(event.target.value);
          setConfirming(false);
          setGenerateError(undefined);
          if (send.isError) send.reset();
        }}
        readOnly={writing}
        aria-busy={writing}
        aria-describedby={writing ? `${ids}-writing` : undefined}
        placeholder={writing ? t("drafts.writing") : t("drafts.placeholder")}
        className="field-sizing-content block min-h-40 w-full resize-none bg-transparent px-4 py-3 text-sm leading-relaxed outline-none placeholder:text-muted-foreground focus-visible:bg-accent/20 read-only:text-muted-foreground"
      />

      {instructionOpen && (
        <form
          className="flex flex-col gap-1.5 border-t border-border/60 px-4 py-2.5"
          onSubmit={(event) => {
            event.preventDefault();
            void generate(instruction);
          }}
        >
          <label htmlFor={`${ids}-instruction`} className="text-xs text-muted-foreground">
            {t("drafts.instructionLabel")}
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              ref={instructionInput}
              id={`${ids}-instruction`}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              maxLength={500}
              placeholder={t("drafts.instructionPlaceholder")}
              className="h-8 min-w-0 flex-1 basis-48 text-ui md:text-ui"
            />
            <div className="flex items-center gap-2">
              <Button type="submit" size="sm">
                {t("drafts.generate")}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setInstructionOpen(false);
                  textarea.current?.focus();
                }}
              >
                {t("drafts.cancel")}
              </Button>
            </div>
          </div>
          {!empty && <p className="text-xs text-muted-foreground">{t("drafts.replacesText")}</p>}
        </form>
      )}

      {(generateError !== undefined || sendError) && (
        <EditorError
          title={generateError !== undefined ? t("drafts.generateFailed") : t("drafts.sendFailed")}
          error={generateError ?? sendError}
          kind={generateError !== undefined ? "generate" : "send"}
        />
      )}

      {confirming ? (
        <fieldset
          aria-labelledby={`${ids}-confirm`}
          className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-border/60 bg-muted/50 px-4 py-2.5"
        >
          <p id={`${ids}-confirm`} className="min-w-0 flex-1 basis-64 text-ui">
            {t("drafts.confirmText")}
          </p>
          <div className="flex items-center gap-2">
            <Button ref={confirmButton} size="sm" onClick={confirmSend}>
              <Send aria-hidden="true" />
              {t("drafts.confirmSend")}
            </Button>
            <Button size="sm" variant="ghost" onClick={keepEditing}>
              {t("drafts.keepEditing")}
            </Button>
          </div>
        </fieldset>
      ) : (
        <footer className="flex flex-wrap items-center gap-2 border-t border-border/60 px-4 py-2">
          {writing ? (
            <>
              <p id={`${ids}-writing`} role="status" className="text-ui text-muted-foreground">
                {t("drafts.writing")}
              </p>
              <Button size="sm" variant="outline" onClick={cancel} className="ml-auto">
                {t("drafts.cancel")}
                {hasKeyboard && <KeyHint keys="escape" />}
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" onClick={requestSend} disabled={busy || empty}>
                <Send aria-hidden="true" />
                {send.isPending ? t("drafts.sending") : t("drafts.send")}
              </Button>
              {hasKeyboard && <KeyHint keys="mod+enter" className="mr-1" />}
              {!instructionOpen && (
                <Button size="sm" variant="outline" onClick={openInstruction} disabled={busy}>
                  <PenLine aria-hidden="true" />
                  {t("drafts.suggest")}
                </Button>
              )}
              <SaveStatus
                state={body === savedText ? saveState : "saving"}
                onRetry={() => void save().catch(() => undefined)}
              />
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => discard.mutate()}
                disabled={busy}
                aria-label={t("drafts.discardDraft")}
                title={t("drafts.discardDraft")}
              >
                <Trash2 />
              </Button>
            </>
          )}
        </footer>
      )}
    </section>
  );
}

function SaveStatus({ state, onRetry }: { state: SaveState; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <span
      role="status"
      className={cn(
        "ml-auto flex items-center gap-2 text-xs text-muted-foreground",
        state === "error" && "text-destructive",
      )}
    >
      {state === "saved" && t("drafts.saved")}
      {state === "saving" && t("drafts.saving")}
      {state === "error" && (
        <>
          {t("drafts.saveFailed")}
          <Button size="xs" variant="outline" onClick={onRetry}>
            {t("drafts.retry")}
          </Button>
        </>
      )}
    </span>
  );
}

function EditorError({
  title,
  error,
  kind,
}: {
  title: string;
  error: unknown;
  kind: "generate" | "send";
}) {
  const { t } = useTranslation();
  let text: string;
  let requestId: string | undefined;
  const code = kind === "generate" ? generateErrorCode(error) : undefined;
  if (code) {
    text = t(`drafts.generateErrors.${code}`);
  } else {
    const key = kind === "send" ? sendErrorKey(error) : undefined;
    const described = describeApiError(error, t);
    text = key ? t(`drafts.sendErrors.${key}`) : described.title;
    requestId = described.description;
  }
  return (
    <div
      role="alert"
      className="flex items-start gap-2 border-t border-border/60 px-4 py-2.5 text-ui"
    >
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0">
        <p className="font-medium text-destructive">{title}</p>
        <p className="text-muted-foreground">{text}</p>
        {requestId && <p className="text-xs text-muted-foreground">{requestId}</p>}
      </div>
    </div>
  );
}
