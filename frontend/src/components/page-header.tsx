import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: string;
  /** Short secondary information next to the title, e.g. a count. */
  meta?: ReactNode;
  /** Controls on the right (buttons, filters). */
  actions?: ReactNode;
  /** Content before the title, e.g. a back button in stacked mobile views. */
  leading?: ReactNode;
  className?: string;
}

export function PageHeader({ title, meta, actions, leading, className }: PageHeaderProps) {
  return (
    <header
      className={cn(
        "flex h-header shrink-0 items-center gap-2 border-b px-4 md:px-5",
        leading && "pl-2 md:pl-3",
        className,
      )}
    >
      {leading}
      <h1 className="truncate text-sm font-medium">{title}</h1>
      {meta && <span className="text-ui text-muted-foreground">{meta}</span>}
      {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
    </header>
  );
}
