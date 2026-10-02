import { useQuery } from "@tanstack/react-query";
import { Link, useRouterState } from "@tanstack/react-router";
import { Mails } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import { mailboxesQueryOptions } from "@/api/mail";

/** Link options for the main inbox entry: inactive while a shared mailbox is open. */
export function useInboxActiveOptions() {
  const location = useRouterState({ select: (state) => state.location });
  const mailboxes = useQuery(mailboxesQueryOptions);
  const selected = (location.search as { mailbox?: unknown }).mailbox;
  const shared =
    location.pathname === "/inbox" &&
    !!mailboxes.data?.some((mailbox) => mailbox.id === selected && mailbox.is_shared);
  // The search of the open inbox then never equals the plain inbox link.
  return shared ? { exact: true, includeSearch: true } : undefined;
}

/**
 * Shared mailboxes the user may read, apart from the main navigation: each opens the inbox
 * filtered to it. Nothing is rendered without shared mailboxes.
 */
export function SharedMailboxNav({
  className,
  linkClassName,
  headingClassName,
  onNavigate,
}: {
  className?: string;
  linkClassName: string;
  headingClassName: string;
  onNavigate?: () => void;
}) {
  const { t } = useTranslation();
  const headingId = useId();
  const mailboxes = useQuery(mailboxesQueryOptions);
  const shared = mailboxes.data?.filter((mailbox) => mailbox.is_shared) ?? [];
  if (shared.length === 0) return null;

  return (
    <section aria-labelledby={headingId} className={className}>
      <h2 id={headingId} className={headingClassName}>
        {t("nav.shared")}
      </h2>
      <ul className="flex flex-col gap-px">
        {shared.map((mailbox) => (
          <li key={mailbox.id}>
            <Link
              to="/inbox"
              search={{ mailbox: mailbox.id }}
              activeOptions={{ includeSearch: true }}
              onClick={onNavigate}
              className={linkClassName}
              title={mailbox.address}
            >
              <Mails className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{mailbox.display_name}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
