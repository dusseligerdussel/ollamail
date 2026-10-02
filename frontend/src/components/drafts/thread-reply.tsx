import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PenLine, Reply, ReplyAll } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  createDraft,
  type Draft,
  draftKeys,
  messageDraftsQueryOptions,
  updateDraft,
} from "@/api/drafts";
import { mailboxesQueryOptions } from "@/api/mail";
import { useCommands } from "@/components/command-palette/command-provider";
import { ReplyEditor, type ReplyEditorHandle } from "@/components/drafts/reply-editor";
import { KeyHint } from "@/components/key-hint";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { mediaQueries, useMediaQuery } from "@/hooks/use-media-query";
import type { Command } from "@/lib/commands";

type Then = "focus" | "suggest";

/**
 * "Reply" / "Reply all" below the thread and the reply editor (#93). An open draft of the
 * mail is shown right away, so a draft started earlier (or opened from the drafts overview)
 * continues here. Only for mailboxes the user may send from.
 */
export function ThreadReply({ messageId, mailboxId }: { messageId: string; mailboxId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const hasKeyboard = useMediaQuery(mediaQueries.keyboard);
  const mailboxes = useQuery(mailboxesQueryOptions);
  const canSend = !!mailboxes.data
    ?.find((mailbox) => mailbox.id === mailboxId)
    ?.permissions?.includes("act");
  const existing = useQuery({
    ...messageDraftsQueryOptions(messageId),
    enabled: canSend,
    // Without the earlier draft the reply buttons still work.
    meta: { errorToast: false },
  });

  // `undefined`: show the stored draft, if any; `null`: closed after sending or discarding.
  const [draft, setDraft] = useState<Draft | null | undefined>(undefined);
  const current = draft === undefined ? existing.data?.[0] : (draft ?? undefined);
  const editor = useRef<ReplyEditorHandle>(null);
  const pending = useRef<Then | null>(null);

  const invalidate = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: draftKeys.all }),
    [queryClient],
  );
  const create = useMutation({
    mutationFn: (replyAll: boolean) => createDraft(messageId, replyAll),
    onSuccess: (created) => {
      setDraft(created);
      invalidate();
    },
    onError: () => {
      pending.current = null;
    },
  });
  const changeReplyAll = useMutation({
    mutationFn: ({ id, replyAll }: { id: string; replyAll: boolean }) =>
      updateDraft(id, { reply_all: replyAll }),
    onSuccess: setDraft,
  });

  const open = useCallback(
    (replyAll: boolean, then: Then = "focus") => {
      if (current) {
        if (current.reply_all !== replyAll) {
          changeReplyAll.mutate({ id: current.id, replyAll });
        }
        if (then === "suggest") editor.current?.suggest();
        else editor.current?.focus();
        return;
      }
      if (create.isPending) return;
      pending.current = then;
      create.mutate(replyAll);
    },
    [current, create, changeReplyAll],
  );

  // A new draft focuses its editor (or the instruction field) once it is shown.
  const currentId = current?.id;
  useEffect(() => {
    if (!currentId || !pending.current) return;
    const then = pending.current;
    pending.current = null;
    if (then === "suggest") editor.current?.suggest();
    else editor.current?.focus();
  }, [currentId]);

  const onDone = useCallback(
    (outcome: "sent" | "discarded") => {
      setDraft(null);
      invalidate();
      toast.success(outcome === "sent" ? t("drafts.sent") : t("drafts.discarded"));
    },
    [invalidate, t],
  );
  const onReplyAllChange = useCallback(
    (replyAll: boolean) => {
      if (current) changeReplyAll.mutate({ id: current.id, replyAll });
    },
    [current, changeReplyAll],
  );

  useShortcut(
    {
      id: "drafts.reply",
      keys: "r",
      group: "list",
      description: t("drafts.reply"),
      enabled: canSend,
    },
    () => open(false),
  );
  useShortcut(
    {
      id: "drafts.replyAll",
      keys: "a",
      group: "list",
      description: t("drafts.replyAll"),
      enabled: canSend,
    },
    () => open(true),
  );

  const commands = useMemo<Command[]>(() => {
    if (!canSend) return [];
    const list: Command[] = [
      {
        id: "drafts.reply",
        label: t("drafts.reply"),
        group: "actions",
        icon: Reply,
        shortcut: "r",
        run: () => open(false),
      },
      {
        id: "drafts.replyAll",
        label: t("drafts.replyAll"),
        group: "actions",
        icon: ReplyAll,
        shortcut: "a",
        run: () => open(true),
      },
    ];
    // With an open editor, the editor registers "Suggest draft" itself.
    if (!current) {
      list.push({
        id: "drafts.suggest",
        label: t("drafts.suggest"),
        group: "actions",
        icon: PenLine,
        run: () => open(false, "suggest"),
      });
    }
    return list;
  }, [canSend, current, t, open]);
  useCommands(commands);

  if (!canSend) return null;
  if (current) {
    return (
      <ReplyEditor
        key={current.id}
        ref={editor}
        draft={current}
        messageId={messageId}
        onDraftChange={setDraft}
        onReplyAllChange={onReplyAllChange}
        onDone={onDone}
      />
    );
  }
  if (create.isPending) {
    return (
      <div role="status" aria-busy="true" className="rounded-lg border p-4">
        <span className="sr-only">{t("common.loading")}</span>
        <Skeleton className="mb-3 h-3 w-48" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => open(false)}>
        <Reply aria-hidden="true" />
        {t("drafts.reply")}
        {hasKeyboard && <KeyHint keys="r" />}
      </Button>
      <Button variant="outline" size="sm" onClick={() => open(true)}>
        <ReplyAll aria-hidden="true" />
        {t("drafts.replyAll")}
        {hasKeyboard && <KeyHint keys="a" />}
      </Button>
    </div>
  );
}
