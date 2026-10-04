import { cn } from "cn";
import { useTranslation } from "react-i18next";

import { Skeleton } from "@/components/ui/skeleton";

// Fixed widths keep the placeholder stable between renders while still looking like real rows.
const rowWidths = [
  ["w-24", "w-3/5"],
  ["w-32", "w-2/5"],
  ["w-20", "w-1/2"],
  ["w-28", "w-3/4"],
  ["w-24", "w-1/3"],
  ["w-36", "w-1/2"],
] as const;

/**
 * Loading placeholder for dense lists (one row per item).
 *
 * `mail` mirrors the message list: two lines per row on phones (60 px, see `message-list.tsx`) and
 * one line from the `md` breakpoint on, so the list does not jump once the messages arrive.
 */
export function ListSkeleton({ rows = 8, mail = false }: { rows?: number; mail?: boolean }) {
  const { t } = useTranslation();

  return (
    <div role="status" aria-live="polite" aria-busy="true" className="flex flex-col">
      <span className="sr-only">{t("common.loading")}</span>
      {Array.from({ length: rows }, (_, index) => {
        const [sender, subject] = rowWidths[index % rowWidths.length] ?? rowWidths[0];
        return (
          <div
            // biome-ignore lint/suspicious/noArrayIndexKey: placeholder rows have no identity
            key={index}
            data-testid="list-skeleton-row"
            className={cn(
              "flex border-b border-border/60 px-4",
              mail
                ? "h-15 flex-col justify-center gap-2 md:h-row md:flex-row md:items-center md:gap-4"
                : "h-row items-center gap-4",
            )}
          >
            <div className={cn("flex shrink-0 items-center gap-4", mail && "md:contents")}>
              <Skeleton className={`h-3 shrink-0 ${sender}`} />
              {mail && <div className="flex-1 md:hidden" />}
              {mail && <Skeleton className="h-3 w-10 shrink-0 md:hidden" />}
            </div>
            <div className={cn("flex-1", mail && "flex-none pl-4 md:flex-1 md:pl-0")}>
              <Skeleton className={`h-3 ${subject}`} />
            </div>
            <Skeleton className={cn("h-3 w-10 shrink-0", mail && "hidden md:block")} />
          </div>
        );
      })}
    </div>
  );
}
