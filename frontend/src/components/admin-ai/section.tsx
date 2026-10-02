import type { ReactNode } from "react";

/** Titled group of rows, as on the settings page. */
export function AdminSection({
  id,
  title,
  actions,
  children,
}: {
  id: string;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id}>
      <div className="mb-2 flex min-h-8 items-end justify-between gap-2">
        <h2 id={id} className="text-xs font-medium text-muted-foreground">
          {title}
        </h2>
        {actions}
      </div>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  );
}
