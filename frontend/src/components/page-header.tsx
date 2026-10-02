import type { ReactNode } from "react";

import { useDocumentTitle } from "@/hooks/use-document-title";
import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: string;
  /** Short secondary information next to the title, e.g. a count. */
  meta?: ReactNode;
  /** Controls on the right (buttons, filters). */
  actions?: ReactNode;
  /** Content before the title, e.g. a back button in stacked mobile views. */
  leading?: ReactNode;
  /** 2 for the detail pane of a split view, whose list pane already has the page's `h1`. */
  headingLevel?: 1 | 2;
  /**
   * Title of the browser tab; defaults to `title` for an `h1`. `false` keeps the current one
   * (for mail subjects, which must not reach the browser history).
   */
  documentTitle?: string | false;
  className?: string;
}

export function PageHeader({
  title,
  meta,
  actions,
  leading,
  headingLevel = 1,
  documentTitle,
  className,
}: PageHeaderProps) {
  useDocumentTitle(
    documentTitle === false
      ? undefined
      : (documentTitle ?? (headingLevel === 1 ? title : undefined)),
  );
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <header
      className={cn(
        "flex h-header shrink-0 items-center gap-2 border-b px-4 md:px-5",
        leading && "pl-2 md:pl-3",
        className,
      )}
    >
      {leading}
      <Heading className="truncate text-sm font-medium">{title}</Heading>
      {meta && <span className="text-ui text-muted-foreground">{meta}</span>}
      {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
    </header>
  );
}
