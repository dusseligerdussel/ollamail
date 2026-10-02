import { Link } from "@tanstack/react-router";
import { Fragment, useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { DigestReference } from "@/api/digest";
import { parseScript } from "@/lib/digest-script";

/**
 * The digest script as text. `[n]` marks become links to the mails they refer to (opened in the
 * inbox thread view). The title heading is left out; the page header shows it.
 */
export function Transcript({
  script,
  references,
}: {
  script: string;
  references: readonly DigestReference[];
}) {
  const { t } = useTranslation();
  const blocks = useMemo(() => {
    const parsed = parseScript(script);
    return parsed[0]?.kind === "heading" ? parsed.slice(1) : parsed;
  }, [script]);
  const byRef = useMemo(
    () => new Map(references.map((reference) => [reference.ref, reference])),
    [references],
  );

  return (
    <section aria-labelledby="digest-transcript" className="flex flex-col gap-3">
      <h2 id="digest-transcript" className="text-xs font-medium text-muted-foreground">
        {t("digest.transcript")}
      </h2>
      <div className="flex max-w-prose flex-col gap-3 text-sm leading-relaxed">
        {blocks.map((block, index) =>
          block.kind === "heading" ? (
            // biome-ignore lint/suspicious/noArrayIndexKey: blocks are static per script
            <h3 key={index} className="mt-2 font-medium">
              {block.text}
            </h3>
          ) : (
            // biome-ignore lint/suspicious/noArrayIndexKey: blocks are static per script
            <p key={index}>
              {block.parts.map((part, partIndex) =>
                "text" in part ? (
                  // biome-ignore lint/suspicious/noArrayIndexKey: parts are static per script
                  <Fragment key={partIndex}>{part.text}</Fragment>
                ) : (
                  // biome-ignore lint/suspicious/noArrayIndexKey: parts are static per script
                  <span key={partIndex} className="ml-0.5 whitespace-nowrap">
                    {part.refs.map((ref) => {
                      const reference = byRef.get(ref);
                      return reference ? (
                        <Link
                          key={ref}
                          to="/inbox"
                          search={{ message: reference.message_id }}
                          aria-label={t("digest.openMail", { ref })}
                          title={t("digest.openMail", { ref })}
                          className="mx-px inline-flex h-4 min-w-4 items-center justify-center rounded-sm border px-1 align-text-top text-[0.6875rem] leading-none text-muted-foreground tabular-nums outline-none hover:border-ring hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/80"
                        >
                          {ref}
                        </Link>
                      ) : null;
                    })}
                  </span>
                ),
              )}
            </p>
          ),
        )}
      </div>
    </section>
  );
}
