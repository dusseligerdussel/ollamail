import { CircleAlert, Info, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

const icons = { info: Info, warning: TriangleAlert, error: CircleAlert };

/** Short boxed hint inside admin pages and sheets; `error` and `warning` are announced. */
export function Notice({
  tone = "info",
  children,
  className,
}: {
  tone?: keyof typeof icons;
  children: ReactNode;
  className?: string;
}) {
  const Icon = icons[tone];
  return (
    <div
      role={tone === "info" ? undefined : "alert"}
      className={cn(
        "flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-ui",
        tone === "error" && "border-destructive/40",
        className,
      )}
    >
      <Icon
        aria-hidden
        className={cn(
          "mt-0.5 size-4 shrink-0",
          tone === "info" ? "text-muted-foreground" : "text-destructive",
        )}
      />
      <div className="min-w-0 text-pretty">{children}</div>
    </div>
  );
}
