import { TriageLabel } from "@/components/triage/triage-label";
import { TriageReason } from "@/components/triage/triage-reason";

/**
 * Extension points of the mail views. They render nothing yet; the features fill them:
 *
 * - `TriageLabelSlot` (#21): the triage category of a message, in the list row and above the
 *   thread. Keep it a single, quiet label (`docs/DESIGN.md`).
 * - `MessageTasksSlot` (#23): "Tasks from this message" below the thread.
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

export function MessageTasksSlot(_: SlotProps) {
  return null;
}
