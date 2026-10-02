import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  beforeLoad: ({ search }) => {
    // The Gmail connect flow returns here (`?mailbox_connected=<id>` or `?mailbox_error=<code>`).
    const result = search as Record<string, unknown>;
    if (result.mailbox_connected !== undefined || result.mailbox_error !== undefined) {
      throw redirect({
        to: "/settings/mailboxes",
        search:
          typeof result.mailbox_error === "string"
            ? { error: result.mailbox_error }
            : { connected: true },
        replace: true,
      });
    }
    throw redirect({ to: "/inbox", replace: true });
  },
});
