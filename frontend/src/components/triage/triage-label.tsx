import { createContext, type ReactNode, useContext } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

import { useCategoryName, useMessageTriage } from "./use-triage";

const LabelsHidden = createContext(false);

/** Hides the labels below, e.g. in the inbox grouped by category, where they repeat the group. */
export function HideTriageLabels({ hidden, children }: { hidden: boolean; children: ReactNode }) {
  return <LabelsHidden value={hidden}>{children}</LabelsHidden>;
}

/**
 * The category of a message as a quiet label in a list row. Nothing while the message is not
 * triaged yet or its category is hidden; high priority is set slightly stronger.
 */
export function TriageLabel({ messageId }: { messageId: string }) {
  const hidden = useContext(LabelsHidden);
  if (hidden) return null;
  return <Label messageId={messageId} />;
}

function Label({ messageId }: { messageId: string }) {
  const { t } = useTranslation();
  const categoryName = useCategoryName();
  const { triage, category } = useMessageTriage(messageId);
  if (!triage || !category || category.hidden) return null;
  const name = categoryName(category);
  const high = triage.priority === 1;
  return (
    <span
      data-testid="triage-label"
      data-priority={triage.priority}
      title={t(high ? "triage.labelHighPriority" : "triage.label", { category: name })}
      className={cn(
        "max-w-32 shrink-0 truncate rounded-sm border px-1.5 text-xs leading-[1.125rem]",
        high ? "border-foreground/25 font-medium text-foreground" : "text-muted-foreground",
      )}
    >
      <span className="sr-only">
        {t(high ? "triage.labelHighPriority" : "triage.label", { category: name })}
      </span>
      <span aria-hidden="true">{name}</span>
    </span>
  );
}
