import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  createMailbox,
  type MailboxProvider,
  type MailboxType,
  mailboxProvidersQueryOptions,
  startOAuthConnect,
} from "@/api/mail";
import { InlineError } from "@/components/inline-error";
import { ImapForm } from "@/components/mail/imap-form";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";

export const Route = createFileRoute("/settings_/mailboxes_/new")({
  component: NewMailboxPage,
});

const RETURN_PATH = "/settings/mailboxes";

function NewMailboxPage() {
  const { t } = useTranslation();
  const providers = useQuery(mailboxProvidersQueryOptions);
  const [selected, setSelected] = useState<MailboxType>();
  const type = selected ?? providers.data?.[0]?.type;
  const provider = providers.data?.find((item) => item.type === type);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  return (
    <>
      <PageHeader
        title={t("mailboxes.addTitle")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings/mailboxes" aria-label={t("mailboxes.backToList")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {providers.isPending && <Skeleton className="h-24 w-full" />}
          {providers.isError && <InlineError error={providers.error} />}
          {providers.data && providers.data.length === 0 && (
            <p className="text-ui text-muted-foreground">{t("mailboxes.noProviders")}</p>
          )}
          {providers.data && providers.data.length > 1 && (
            <ProviderChoice providers={providers.data} value={type} onChange={setSelected} />
          )}
          {provider?.connect === "credentials" && (
            <ImapForm
              onCreate={createMailbox}
              onCreated={(mailbox) => {
                void queryClient.invalidateQueries({ queryKey: ["mailbox"] });
                toast.success(t("mailboxes.created", { name: mailbox.display_name }));
                void navigate({ to: "/settings/mailboxes" });
              }}
              onPreferOAuth={(native) =>
                providers.data?.some((item) => item.type === native) && setSelected(native)
              }
              oauthTypes={providers.data?.filter((p) => p.connect === "oauth").map((p) => p.type)}
            />
          )}
          {provider?.connect === "oauth" && <OAuthConnect provider={provider} />}
        </div>
      </div>
    </>
  );
}

function ProviderChoice({
  providers,
  value,
  onChange,
}: {
  providers: MailboxProvider[];
  value: MailboxType | undefined;
  onChange: (type: MailboxType) => void;
}) {
  const { t } = useTranslation();
  const labelId = useId();
  return (
    <section aria-labelledby={labelId} className="mb-8">
      <h2 id={labelId} className="mb-2 text-xs font-medium text-muted-foreground">
        {t("mailboxes.providerLabel")}
      </h2>
      <RadioGroup
        aria-labelledby={labelId}
        value={value}
        onValueChange={(next) => onChange(next as MailboxType)}
        className="gap-0 divide-y rounded-lg border"
      >
        {providers.map((provider) => {
          const id = `${labelId}-${provider.type}`;
          return (
            <div key={provider.type} className="flex items-start gap-3 px-4 py-3">
              <RadioGroupItem id={id} value={provider.type} className="mt-0.5" />
              <Label htmlFor={id} className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
                <span className="text-ui font-medium">
                  {t(`mailboxes.providers.${provider.type}.title`)}
                </span>
                <span className="text-ui font-normal text-muted-foreground">
                  {t(`mailboxes.providers.${provider.type}.description`)}
                </span>
              </Label>
            </div>
          );
        })}
      </RadioGroup>
    </section>
  );
}

function OAuthConnect({ provider }: { provider: MailboxProvider }) {
  const { t } = useTranslation();
  const connect = useMutation({
    mutationFn: () => startOAuthConnect(provider.oauth_start_path ?? "", RETURN_PATH),
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  return (
    <section className="rounded-lg border px-4 py-4">
      <p className="text-ui text-muted-foreground">
        {t(`mailboxes.providers.${provider.type}.connectHint`)}
      </p>
      <Button
        className="mt-4"
        size="sm"
        disabled={connect.isPending}
        onClick={() => connect.mutate()}
      >
        <ExternalLink />
        {t(`mailboxes.providers.${provider.type}.connect`)}
      </Button>
    </section>
  );
}
