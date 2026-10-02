import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { createSharedMailbox } from "@/api/shared-mailboxes";
import { Forbidden } from "@/components/forbidden";
import { ImapForm } from "@/components/mail/imap-form";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";

export const Route = createFileRoute("/admin_/shared-mailboxes_/new")({
  component: NewSharedMailboxPage,
});

function NewSharedMailboxPage() {
  const { t } = useTranslation();
  const { isAdmin } = useCurrentUser();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  if (!isAdmin) return <Forbidden />;

  return (
    <>
      <PageHeader
        title={t("pages.sharedMailboxes.addTitle")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/admin/shared-mailboxes" aria-label={t("pages.sharedMailboxes.backToList")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {/* Shared mailboxes are connected with credentials (IMAP); assignments follow. */}
          <ImapForm
            onCreate={(body) => createSharedMailbox({ ...body, users: [], groups: [] })}
            onCreated={(mailbox) => {
              void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
              toast.success(t("pages.sharedMailboxes.created", { name: mailbox.display_name }));
              void navigate({
                to: "/admin/shared-mailboxes/$mailboxId",
                params: { mailboxId: mailbox.id },
              });
            }}
            description={t("pages.sharedMailboxes.formDescription")}
            submitLabel={t("pages.sharedMailboxes.add")}
          />
        </div>
      </div>
    </>
  );
}
