import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { ArrowLeft, Fingerprint, type KeyRound, ListChecks, Plus, Smartphone } from "lucide-react";
import { type ReactNode, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { describeApiError, isApiError } from "@/api/errors";
import {
  MFA_REQUIRED,
  type MfaEnrolled,
  type MfaStatus,
  mfaQueryKey,
  mfaStatusQueryOptions,
  type Passkey,
  regenerateRecoveryCodes,
  removePasskey,
  removeTotp,
} from "@/api/mfa";
import { isReauthCancelled } from "@/api/reauth";
import { PasskeyForm } from "@/components/account/security/passkey-form";
import { RecoveryCodeList } from "@/components/account/security/recovery-code-list";
import { TotpSetup } from "@/components/account/security/totp-setup";
import { Notice } from "@/components/admin/notice";
import { useReauth } from "@/components/auth/reauth";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useCurrentUser } from "@/hooks/use-current-user";
import { webauthnSupported } from "@/lib/webauthn";

export const Route = createFileRoute("/settings_/security")({
  component: SecurityPage,
});

type Panel = "passkey" | "totp" | "codes" | "regenerate";

function SecurityPage() {
  const { t } = useTranslation();
  const status = useQuery(mfaStatusQueryOptions);
  const queryClient = useQueryClient();
  const [panel, setPanel] = useState<Panel>();
  const [codes, setCodes] = useState<string[]>();

  // After a new factor: show the recovery codes if this was the first one.
  const enrolled = (message: string) => (result: MfaEnrolled) => {
    void queryClient.invalidateQueries({ queryKey: mfaQueryKey });
    toast.success(message);
    if (result.recovery_codes) {
      setCodes(result.recovery_codes);
      setPanel("codes");
    } else {
      setPanel(undefined);
    }
  };

  return (
    <>
      <PageHeader
        title={t("account.security.title")}
        leading={
          <Button asChild size="icon-sm" variant="ghost">
            <Link to="/settings" aria-label={t("account.security.back")}>
              <ArrowLeft />
            </Link>
          </Button>
        }
      />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-4 py-6 md:px-6 md:py-8">
          {status.isPending && <ListSkeleton />}
          {status.isError && <InlineError error={status.error} />}
          {status.data && (
            <SecurityOverview status={status.data} onOpen={(next) => setPanel(next)} />
          )}
        </div>
      </div>

      <SidePanel
        open={panel === "passkey"}
        onClose={() => setPanel(undefined)}
        title={t("account.security.passkeys.sheetTitle")}
        description={t("account.security.passkeys.sheetDescription")}
      >
        <PasskeyForm onEnrolled={enrolled(t("account.security.passkeys.created"))} />
      </SidePanel>
      <SidePanel
        open={panel === "totp"}
        onClose={() => setPanel(undefined)}
        title={t("account.security.totp.sheetTitle")}
        description={t("account.security.totp.sheetDescription")}
      >
        <TotpSetup onEnrolled={enrolled(t("account.security.totp.enabled"))} />
      </SidePanel>
      <RegenerateCodesPanel
        open={panel === "regenerate"}
        onClose={() => setPanel(undefined)}
        onCreated={(next) => {
          void queryClient.invalidateQueries({ queryKey: mfaQueryKey });
          setCodes(next);
          setPanel("codes");
        }}
      />
      <SidePanel
        open={panel === "codes" && !!codes}
        onClose={() => {
          setPanel(undefined);
          setCodes(undefined);
        }}
        title={t("account.security.recovery.sheetTitle")}
        description={t("account.security.recovery.sheetDescription")}
        footer={
          <Button
            onClick={() => {
              setPanel(undefined);
              setCodes(undefined);
            }}
          >
            {t("account.security.recovery.done")}
          </Button>
        }
      >
        {codes && <RecoveryCodeList codes={codes} />}
      </SidePanel>
    </>
  );
}

function SecurityOverview({
  status,
  onOpen,
}: {
  status: MfaStatus;
  onOpen: (panel: Panel) => void;
}) {
  const { t } = useTranslation();
  if (!status.available) {
    return <Notice>{t("account.security.unavailable")}</Notice>;
  }
  const active = status.totp || status.passkeys.length > 0;
  let summary = t("account.security.off");
  if (status.enforced) summary = t("account.security.enforced");
  else if (active) summary = t("account.security.on");

  return (
    <div className="flex flex-col gap-8">
      <p className="text-ui text-muted-foreground">{summary}</p>
      <PasskeysSection status={status} onAdd={() => onOpen("passkey")} />
      <Section id="security-more" title={t("account.security.more")}>
        <TotpRow status={status} onSetup={() => onOpen("totp")} />
        <Row
          icon={ListChecks}
          title={t("account.security.recovery.title")}
          description={
            active
              ? `${t("account.security.recovery.remaining", {
                  count: status.recovery_codes_remaining,
                })} · ${t("account.security.recovery.description")}`
              : t("account.security.recovery.none")
          }
        >
          {active && (
            <Button
              variant="outline"
              size="sm"
              className="text-ui"
              onClick={() => onOpen("regenerate")}
            >
              {t("account.security.recovery.regenerate")}
            </Button>
          )}
        </Row>
      </Section>
    </div>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id}>
      <h2 id={id} className="mb-2 text-xs font-medium text-muted-foreground">
        {title}
      </h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  );
}

function Row({
  icon: Icon,
  title,
  description,
  badge,
  children,
}: {
  icon: typeof KeyRound;
  title: string;
  description: string;
  badge?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3.5">
      <Icon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-ui font-medium">{title}</span>
          {badge}
        </div>
        <div className="text-ui text-muted-foreground">{description}</div>
      </div>
      {children && <div className="flex shrink-0 gap-2">{children}</div>}
    </div>
  );
}

function useRemovalError() {
  const { t } = useTranslation();
  return (error: unknown) =>
    isApiError(error) && error.problem?.type === MFA_REQUIRED
      ? t("account.security.lastFactor")
      : describeApiError(error, t).title;
}

function useDate() {
  const { i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  return (value: string) => {
    try {
      return new Intl.DateTimeFormat(i18n.resolvedLanguage, {
        dateStyle: "medium",
        timeZone: timezone,
      }).format(new Date(value));
    } catch {
      return new Date(value).toLocaleDateString(i18n.resolvedLanguage);
    }
  };
}

function PasskeysSection({ status, onAdd }: { status: MfaStatus; onAdd: () => void }) {
  const { t } = useTranslation();
  const supported = webauthnSupported();
  let note: string | undefined;
  if (!status.passkeys_configured) note = t("account.security.passkeys.notConfigured");
  else if (!supported) note = t("account.security.passkeys.unsupported");

  return (
    <Section id="security-passkeys" title={t("account.security.passkeys.title")}>
      <div className="flex flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-ui text-muted-foreground">
          {note ?? t("account.security.passkeys.description")}
        </p>
        <Button
          size="sm"
          variant="outline"
          className="shrink-0 self-start text-ui sm:self-auto"
          disabled={!!note}
          onClick={onAdd}
        >
          <Plus aria-hidden />
          {t("account.security.passkeys.add")}
        </Button>
      </div>
      {status.passkeys.length === 0 ? (
        <p className="px-4 py-3.5 text-ui text-muted-foreground">
          {t("account.security.passkeys.empty")}
        </p>
      ) : (
        <ul className="divide-y">
          {status.passkeys.map((passkey) => (
            <PasskeyRow key={passkey.id} passkey={passkey} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function PasskeyRow({ passkey }: { passkey: Passkey }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formatDate = useDate();
  const removalError = useRemovalError();
  const withReauth = useReauth();
  const remove = useMutation({
    mutationFn: () => withReauth(() => removePasskey(passkey.id)),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: mfaQueryKey });
      toast.success(t("account.security.passkeys.removed"));
    },
    onError: (error) => {
      if (!isReauthCancelled(error)) toast.error(removalError(error));
    },
  });
  const added = formatDate(passkey.created_at);
  const description = passkey.last_used_at
    ? t("account.security.passkeys.metaUsed", { added, used: formatDate(passkey.last_used_at) })
    : t("account.security.passkeys.meta", { added });

  return (
    <li>
      <Row
        icon={Fingerprint}
        title={passkey.name}
        description={description}
        badge={
          passkey.backed_up && (
            <Badge variant="secondary" className="shrink-0 font-normal">
              {t("account.security.passkeys.synced")}
            </Badge>
          )
        }
      >
        <Button
          variant="ghost"
          size="sm"
          className="text-ui"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
          aria-label={t("account.security.passkeys.removeLabel", { name: passkey.name })}
        >
          {t("account.security.passkeys.remove")}
        </Button>
      </Row>
    </li>
  );
}

function TotpRow({ status, onSetup }: { status: MfaStatus; onSetup: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const removalError = useRemovalError();
  const withReauth = useReauth();
  const remove = useMutation({
    mutationFn: () => withReauth(removeTotp),
    meta: { errorToast: false },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: mfaQueryKey });
      toast.success(t("account.security.totp.removed"));
    },
    onError: (error) => {
      if (!isReauthCancelled(error)) toast.error(removalError(error));
    },
  });
  return (
    <Row
      icon={Smartphone}
      title={t("account.security.totp.title")}
      description={t("account.security.totp.description")}
      badge={
        status.totp && (
          <Badge variant="secondary" className="shrink-0 font-normal">
            {t("account.security.totp.active")}
          </Badge>
        )
      }
    >
      {status.totp ? (
        <Button
          variant="ghost"
          size="sm"
          className="text-ui"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          {t("account.security.totp.remove")}
        </Button>
      ) : (
        <Button variant="outline" size="sm" className="text-ui" onClick={onSetup}>
          {t("account.security.totp.setup")}
        </Button>
      )}
    </Row>
  );
}

function RegenerateCodesPanel({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (codes: string[]) => void;
}) {
  const { t } = useTranslation();
  const withReauth = useReauth();
  const create = useMutation({
    mutationFn: () => withReauth(regenerateRecoveryCodes),
    onSuccess: (result) => onCreated(result.codes),
  });
  return (
    <SidePanel
      open={open}
      onClose={onClose}
      title={t("account.security.recovery.sheetTitle")}
      description={t("account.security.recovery.regenerateDescription")}
      footer={
        <Button disabled={create.isPending} onClick={() => create.mutate()}>
          {t("account.security.recovery.create")}
        </Button>
      }
    />
  );
}

/** Right-hand sheet with a title, a short intro and optional content and footer. */
function SidePanel({
  open,
  onClose,
  title,
  description,
  footer,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description: string;
  footer?: ReactNode;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <Sheet open={open} onOpenChange={(next) => !next && onClose()}>
      <SheetContent
        side="right"
        closeLabel={t("common.close")}
        className="w-full gap-0 sm:max-w-md"
        aria-describedby={`${id}-intro`}
      >
        <SheetHeader className="border-b">
          <SheetTitle>{title}</SheetTitle>
          <SheetDescription id={`${id}-intro`}>{description}</SheetDescription>
        </SheetHeader>
        {children && <div className="flex-1 overflow-y-auto p-4">{children}</div>}
        {footer && <SheetFooter className="border-t">{footer}</SheetFooter>}
      </SheetContent>
    </Sheet>
  );
}
