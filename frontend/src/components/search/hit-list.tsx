import { Link } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import type { SearchHit } from "@/api/search";
import { useCurrentUser } from "@/hooks/use-current-user";
import { addressName, formatListDate } from "@/lib/mail-format";
import { cn } from "@/lib/utils";

import { AttachmentSource, isAttachmentSource } from "./attachment-source";
import { HighlightedText } from "./highlighted-text";

interface HitListProps {
  hits: SearchHit[];
  terms: readonly string[];
  selectedId?: string;
  activeIndex: number;
  /** Search params of the link that opens a hit. */
  linkSearch: (hit: SearchHit) => Record<string, unknown>;
  onOpen: (hit: SearchHit) => void;
}

/** Hits of the classic search: sender, date, subject and the excerpt with marked terms. */
export function HitList({
  hits,
  terms,
  selectedId,
  activeIndex,
  linkSearch,
  onOpen,
}: HitListProps) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const list = useRef<HTMLUListElement>(null);

  // Keep the row chosen with `j`/`k` visible.
  useEffect(() => {
    if (activeIndex < 0) return;
    list.current?.children[activeIndex]?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex]);

  return (
    <ul ref={list} aria-label={t("search.hits")} className="flex flex-col">
      {hits.map((hit, index) => {
        const selected = hit.message_id === selectedId;
        return (
          <li key={hit.message_id} className="border-b last:border-b-0">
            <Link
              to="/search"
              search={linkSearch(hit)}
              onClick={() => onOpen(hit)}
              aria-current={selected ? "true" : undefined}
              data-active={index === activeIndex || undefined}
              className={cn(
                "block px-4 py-2.5 text-ui outline-none hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:ring-inset md:px-5",
                "data-[active]:bg-accent/60",
                selected && "bg-accent",
              )}
            >
              <div className="flex items-baseline gap-3">
                <span className="min-w-0 flex-1 truncate font-medium">
                  {addressName(hit.sender)}
                </span>
                <time
                  dateTime={hit.date}
                  className="shrink-0 text-xs text-muted-foreground tabular-nums"
                >
                  {formatListDate(hit.date, i18n.language, timezone)}
                </time>
              </div>
              <div className="truncate">
                <HighlightedText text={hit.subject || t("mail.noSubject")} terms={terms} />
              </div>
              {isAttachmentSource(hit.source) && (
                <AttachmentSource
                  source={hit.source}
                  filename={hit.attachment_filename}
                  className="mt-0.5"
                />
              )}
              {hit.excerpt && (
                <p className="mt-0.5 line-clamp-2 text-muted-foreground [overflow-wrap:anywhere]">
                  <HighlightedText text={hit.excerpt} terms={terms} />
                </p>
              )}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
