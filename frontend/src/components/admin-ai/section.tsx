import type { ReactNode } from "react";

/** Titled group of rows, as on the settings page. */
export function AdminSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id}>
      <h2 id={id} className="mb-2 text-xs font-medium text-muted-foreground">
        {title}
      </h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  );
}
