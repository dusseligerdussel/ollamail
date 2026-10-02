import { useMutation } from "@tanstack/react-query";
import { CircleCheck, CircleX, PlugZap } from "lucide-react";
import { type FormEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { lookupLdapUser, testLdapDirectory, testOidcProvider } from "@/api/admin-auth";
import { Notice } from "@/components/admin/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function Status({ ok, children }: { ok: boolean; children: string }) {
  const Icon = ok ? CircleCheck : CircleX;
  return (
    <div className="flex items-start gap-2 text-ui">
      <Icon
        aria-hidden
        className={
          ok
            ? "mt-0.5 size-4 shrink-0 text-muted-foreground"
            : "mt-0.5 size-4 shrink-0 text-destructive"
        }
      />
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

/** "Test connection" for an OIDC provider: discovery document and signing keys. */
export function OidcTestPanel({ name }: { name: string }) {
  const { t } = useTranslation();
  const test = useMutation({ mutationFn: () => testOidcProvider(name) });
  const result = test.data;
  return (
    <div className="flex flex-col gap-3">
      <Button
        type="button"
        variant="outline"
        className="self-start"
        disabled={test.isPending}
        onClick={() => test.mutate()}
      >
        <PlugZap aria-hidden />
        {test.isPending ? t("pages.signIn.provider.testing") : t("pages.signIn.provider.test")}
      </Button>
      {result && (
        <div aria-live="polite" className="flex flex-col gap-1.5">
          {result.ok ? (
            <>
              <Status ok>
                {t("pages.signIn.provider.testOkOidc", { count: result.signing_keys })}
              </Status>
              {result.authorization_endpoint && (
                <p className="truncate pl-6 font-mono text-xs text-muted-foreground">
                  {result.authorization_endpoint}
                </p>
              )}
              <p className="pl-6 text-xs text-muted-foreground">
                {t("pages.signIn.provider.testSecretHint")}
              </p>
            </>
          ) : (
            <Status ok={false}>
              {t("pages.signIn.provider.testFailed", { error: result.error ?? "–" })}
            </Status>
          )}
        </div>
      )}
    </div>
  );
}

/** "Test connection" (every server: connect, TLS, service bind) and a user lookup. */
export function LdapTestPanel({ name }: { name: string }) {
  const { t } = useTranslation();
  const id = useId();
  const [login, setLogin] = useState("");
  const test = useMutation({ mutationFn: () => testLdapDirectory(name) });
  const lookup = useMutation({ mutationFn: (value: string) => lookupLdapUser(name, value) });

  function onLookup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (login.trim()) lookup.mutate(login.trim());
  }

  const user = lookup.data;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-3">
        <Button
          type="button"
          variant="outline"
          className="self-start"
          disabled={test.isPending}
          onClick={() => test.mutate()}
        >
          <PlugZap aria-hidden />
          {test.isPending ? t("pages.signIn.provider.testing") : t("pages.signIn.provider.test")}
        </Button>
        {test.data && (
          <ul aria-live="polite" className="flex flex-col gap-1.5">
            {test.data.servers.map((server) => (
              <li key={server.url}>
                <Status ok={server.ok}>
                  {server.ok
                    ? `${server.url} · ${t("pages.signIn.provider.serverOk", { latency: server.latency_ms })}`
                    : `${server.url} · ${t("pages.signIn.provider.testFailed", { error: server.error ?? "–" })}`}
                </Status>
              </li>
            ))}
          </ul>
        )}
      </div>
      <form onSubmit={onLookup} className="flex flex-col gap-2">
        <Label htmlFor={id} className="text-ui">
          {t("pages.signIn.provider.lookupLabel")}
        </Label>
        <div className="flex gap-2">
          <Input
            id={id}
            autoComplete="off"
            value={login}
            onChange={(event) => setLogin(event.target.value)}
          />
          <Button type="submit" variant="outline" className="shrink-0" disabled={lookup.isPending}>
            {t("pages.signIn.provider.lookup")}
          </Button>
        </div>
        {user && (
          <div aria-live="polite" className="flex flex-col gap-1.5 pt-1">
            {user.found ? (
              <>
                <Status ok={user.allowed && !user.disabled}>
                  {t("pages.signIn.provider.lookupResult", {
                    name: user.display_name ?? user.subject ?? "–",
                    email: user.email ?? "–",
                  })}
                </Status>
                <p className="pl-6 text-xs text-muted-foreground">
                  {t("pages.signIn.provider.lookupGroups", { count: user.groups?.length ?? 0 })}
                  {user.role ? ` · ${t(`account.roles.${user.role}`)}` : ""}
                </p>
                {!user.allowed && (
                  <p className="pl-6 text-xs text-destructive">
                    {t("pages.signIn.provider.lookupNotAllowed")}
                  </p>
                )}
              </>
            ) : user.error ? (
              <Notice tone="error">
                {t("pages.signIn.provider.testFailed", { error: user.error })}
              </Notice>
            ) : (
              <Status ok={false}>{t("pages.signIn.provider.lookupNotFound")}</Status>
            )}
          </div>
        )}
      </form>
    </div>
  );
}
