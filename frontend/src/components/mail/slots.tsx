import { ThreadReply } from "@/components/drafts/thread-reply";
import { MessageTasks } from "@/components/tasks/message-tasks";
import { TriageLabel } from "@/components/triage/triage-label";
import { TriageReason } from "@/components/triage/triage-reason";

/**
 * Extension points of the mail views, filled by the features:
 *
 * - `TriageLabelSlot` (#21): the triage category of a message, in the list row and above the
 *   thread. Keep it a single, quiet label (`docs/DESIGN.md`).
 * - `MessageTasksSlot` (#23): "Tasks from this message" below the thread.
 * - `ReplySlot` (#93): "Reply" / "Reply all" and the reply editor right below the thread.
 *
 * The props are what the features need to load their data; the slots keep the layout of the
 * inbox stable when the content arrives.
 */

interface SlotProps {
  messageId: string;
}

export function TriageLabelSlot({ messageId, compact }: SlotProps & { compact?: boolean }) {
  return compact ? <TriageLabel messageId={messageId} /> : <TriageReason messageId={messageId} />;
}

export function MessageTasksSlot({ messageId }: SlotProps) {
  return <MessageTasks messageId={messageId} />;
}

export function ReplySlot({ messageId, mailboxId }: SlotProps & { mailboxId: string }) {
  return <ThreadReply messageId={messageId} mailboxId={mailboxId} />;
}
