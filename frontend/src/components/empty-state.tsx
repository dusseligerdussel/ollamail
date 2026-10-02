import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { useDocumentTitle } from "@/hooks/use-document-title";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: ReactNode;
  /** The one clear next step, usually a single button or link. */
  action?: ReactNode;
  /** Use 1 when the empty state is the whole page (no PageHeader). */
  headingLevel?: 1 | 2;
  className?: string;
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  headingLevel = 2,
  className,
}: EmptyStateProps) {
  // A level-1 empty state is the whole page (403, 404).
  useDocumentTitle(headingLevel === 1 ? title : undefined);
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <div
      className={cn(
        "flex h-full flex-1 flex-col items-center justify-center px-6 py-12 text-center",
        className,
      )}
    >
      <div className="flex max-w-xs flex-col items-center">
        {Icon && (
          <div className="mb-4 flex size-9 items-center justify-center rounded-lg border bg-background text-muted-foreground shadow-xs">
            <Icon className="size-4" aria-hidden="true" />
          </div>
        )}
        <Heading className="text-sm font-medium">{title}</Heading>
        {description && (
          <div className="mt-1 text-ui text-balance text-muted-foreground">{description}</div>
        )}
        {action && <div className="mt-5">{action}</div>}
      </div>
    </div>
  );
}
