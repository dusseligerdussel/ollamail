import { useMemo } from "react";

import { highlightSegments } from "@/lib/search-text";

/** Text with the query terms marked. */
export function HighlightedText({ text, terms }: { text: string; terms: readonly string[] }) {
  const segments = useMemo(() => highlightSegments(text, terms), [text, terms]);
  return segments.map((segment, index) =>
    segment.match ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: segments have no identity besides position
      <mark key={index} className="rounded-sm bg-brand/15 px-px text-foreground">
        {segment.text}
      </mark>
    ) : (
      // biome-ignore lint/suspicious/noArrayIndexKey: segments have no identity besides position
      <span key={index}>{segment.text}</span>
    ),
  );
}
