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

/** Loading placeholder for dense lists (one row per item). */
export function ListSkeleton({ rows = 8 }: { rows?: number }) {
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
            className="flex h-row items-center gap-4 border-b border-border/60 px-4"
          >
            <Skeleton className={`h-3 shrink-0 ${sender}`} />
            <div className="flex-1">
              <Skeleton className={`h-3 ${subject}`} />
            </div>
            <Skeleton className="h-3 w-10 shrink-0" />
          </div>
        );
      })}
    </div>
  );
}
