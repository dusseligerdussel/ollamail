import { Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

/** Header with "back to admin" and a scrollable body, shared by the admin sub-pages. */
export function AdminSubPage({
  title,
  backLabel,
  backTo = "/admin",
  meta,
  actions,
  wide = false,
  children,
}: {
  title: string;
  backLabel: string;
  /** Parent page; default the admin overview. */
  backTo?: "/admin" | "/admin/shared-mailboxes";
  meta?: ReactNode;
  actions?: ReactNode;
  /** Full width (tables) instead of the narrow settings column. */
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <>
      <PageHeader
        title={title}
        meta={meta}
        actions={actions}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to={backTo} aria-label={backLabel}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        {wide ? (
          children
        ) : (
          <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">{children}</div>
        )}
      </div>
    </>
  );
}

/** Titled, bordered group of rows (same look as the settings page). */
export function AdminSection({
  id,
  title,
  description,
  className,
  children,
}: {
  id: string;
  title: string;
  description?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className={className}>
      <h2 id={id} className="mb-2 text-xs font-medium text-muted-foreground">
        {title}
      </h2>
      {description && <p className="mb-2 text-ui text-muted-foreground">{description}</p>}
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  );
}
