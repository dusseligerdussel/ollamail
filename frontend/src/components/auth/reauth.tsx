import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import {
  createContext,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import { logoutTo, pageNavigation } from "@/api/auth";
import { describeApiError, isApiError } from "@/api/errors";
import {
  confirmWithCode,
  confirmWithPasskey,
  confirmWithPassword,
  currentPath,
  isReauthRequired,
  providerReauthUrl,
  ReauthCancelledError,
  type ReauthMethod,
  type ReauthOptions,
  reauthOptionsQueryOptions,
} from "@/api/reauth";
import { FormError, FormField } from "@/components/form-field";
import { InlineError } from "@/components/inline-error";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { isWebauthnAbort, webauthnSupported } from "@/lib/webauthn";

/** Runs `action`; if the server asks for a confirmation first, asks for it and retries. */
export type WithReauth = <T>(action: () => Promise<T>) => Promise<T>;

const ReauthContext = createContext<WithReauth | null>(null);

interface PendingConfirmation {
  resolve: () => void;
  reject: (error: Error) => void;
}

/**
 * Provides `useReauth()` and renders the confirmation sheet. Sensitive endpoints answer 403
 * `reauth-required` when the last sign-in or confirmation is older than the instance's limit
 * (`OLLAMAIL_AUTH_REAUTH_MINUTES`); the sheet confirms and the action runs again.
 */
export function ReauthProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingConfirmation>();
  // Concurrent actions wait for the same confirmation.
  const waiting = useRef<Promise<void>>(undefined);

  const confirmFirst = useCallback(() => {
    waiting.current ??= new Promise<void>((resolve, reject) => {
      setPending({ resolve, reject });
    }).finally(() => {
      waiting.current = undefined;
      setPending(undefined);
    });
    return waiting.current;
  }, []);

  const withReauth = useCallback<WithReauth>(
    async (action) => {
      try {
        return await action();
      } catch (error) {
        if (!isReauthRequired(error)) throw error;
      }
      await confirmFirst();
      return action();
    },
    [confirmFirst],
  );

  return (
    <ReauthContext.Provider value={withReauth}>
      {children}
      {pending && (
        <ReauthSheet
          onConfirmed={pending.resolve}
          onCancel={() => pending.reject(new ReauthCancelledError())}
        />
      )}
    </ReauthContext.Provider>
  );
}

const runDirectly: WithReauth = (action) => action();

/** See `ReauthProvider`. Without the provider (isolated component tests) actions just run. */
export function useReauth(): WithReauth {
  return useContext(ReauthContext) ?? runDirectly;
}

function usableMethods(options: ReauthOptions): ReauthMethod[] {
  return options.methods.filter((method) => method !== "webauthn" || webauthnSupported());
}

function errorMessage(error: unknown, method: ReauthMethod, t: TFunction) {
  if (isWebauthnAbort(error)) return t("auth.reauth.passkeyCancelled");
  if (!isApiError(error)) return t("auth.reauth.passkeyFailed");
  if (error.status === 429) return t("auth.reauth.throttled");
  if (error.status === 400) {
    if (method === "password") return t("auth.reauth.wrongPassword");
    if (method === "totp") return t("auth.reauth.wrongCode");
    return t("auth.reauth.passkeyFailed");
  }
  return describeApiError(error, t).title;
}

/** Right-hand sheet: confirm with password, authenticator code, passkey or a new sign-in. */
export function ReauthSheet({
  onConfirmed,
  onCancel,
}: {
  onConfirmed: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const queryClient = useQueryClient();
  const options = useQuery(reauthOptionsQueryOptions);
  const methods = useMemo(() => (options.data ? usableMethods(options.data) : []), [options.data]);
  const [chosen, setChosen] = useState<ReauthMethod>();
  const method = chosen ?? methods[0];
  const [value, setValue] = useState("");
  const provider = options.data?.provider_display_name ?? "";

  const confirm = useMutation({
    mutationFn: async (selected: ReauthMethod) => {
      if (selected === "password") return confirmWithPassword(value);
      if (selected === "totp") return confirmWithCode(value.trim());
      if (selected === "webauthn") return confirmWithPasskey();
      const back = currentPath();
      if (selected === "sso" && options.data?.login_path) {
        pageNavigation.assign(providerReauthUrl(options.data.login_path, back));
      } else {
        await logoutTo(`/login?${new URLSearchParams({ redirect: back })}`);
      }
      // Navigating away: never resolves.
      return new Promise<never>(() => {});
    },
    meta: { errorToast: false },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: reauthOptionsQueryOptions.queryKey });
      onConfirmed();
    },
  });

  function select(next: ReauthMethod) {
    confirm.reset();
    setValue("");
    setChosen(next);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!method) return;
    if ((method === "password" || method === "totp") && !value.trim()) return;
    confirm.mutate(method);
  }

  const error = confirm.isError && method ? errorMessage(confirm.error, method, t) : undefined;
  const textInput = method === "password" || method === "totp";

  let submitLabel = t("auth.reauth.confirm");
  if (confirm.isPending) {
    submitLabel = method === "webauthn" ? t("auth.reauth.waiting") : t("auth.reauth.confirming");
  } else if (method === "webauthn") submitLabel = t("auth.reauth.usePasskey");
  else if (method === "sso") submitLabel = t("auth.reauth.continueWith", { provider });
  else if (method === "signin") submitLabel = t("auth.reauth.signIn");

  return (
    <Sheet open onOpenChange={(open) => !open && onCancel()}>
      <SheetContent
        side="right"
        closeLabel={t("common.close")}
        className="w-full gap-0 sm:max-w-md"
        aria-describedby={`${id}-intro`}
      >
        <SheetHeader className="border-b">
          <SheetTitle>{t("auth.reauth.title")}</SheetTitle>
          <SheetDescription id={`${id}-intro`}>
            {t("auth.reauth.description", { count: options.data?.reauth_minutes ?? 10 })}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-4">
          {options.isPending && (
            <div className="flex flex-col gap-2" aria-busy="true">
              <span className="sr-only">{t("auth.reauth.loading")}</span>
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-9 w-full" />
            </div>
          )}
          {options.isError && (
            <InlineError
              error={options.error}
              onRetry={options.refetch}
              retrying={options.isFetching}
            />
          )}
          {method && (
            <form id={`${id}-form`} noValidate onSubmit={submit} className="flex flex-col gap-4">
              {method === "password" && (
                <FormField
                  key="password"
                  label={t("auth.reauth.passwordLabel")}
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  autoFocus
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  aria-invalid={error ? true : undefined}
                />
              )}
              {method === "totp" && (
                <FormField
                  key="totp"
                  label={t("auth.reauth.totpLabel")}
                  name="code"
                  autoComplete="one-time-code"
                  inputMode="numeric"
                  maxLength={8}
                  autoFocus
                  className="font-mono tracking-widest"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  aria-invalid={error ? true : undefined}
                />
              )}
              {method === "webauthn" && (
                <p className="text-ui text-muted-foreground">{t("auth.reauth.passkeyHint")}</p>
              )}
              {method === "sso" && (
                <p className="text-ui text-muted-foreground">
                  {t("auth.reauth.ssoHint", { provider })}
                </p>
              )}
              {method === "signin" && (
                <p className="text-ui text-muted-foreground">{t("auth.reauth.signinHint")}</p>
              )}
              {error && <FormError>{error}</FormError>}
            </form>
          )}
          {method && methods.length > 1 && (
            <nav
              aria-label={t("auth.reauth.otherMethods")}
              className="flex flex-col items-start gap-1"
            >
              {methods
                .filter((other) => other !== method)
                .map((other) => (
                  <Button
                    key={other}
                    type="button"
                    variant="link"
                    className="h-auto p-0 text-ui"
                    onClick={() => select(other)}
                  >
                    {t(`auth.reauth.switch.${other}`, { provider })}
                  </Button>
                ))}
            </nav>
          )}
        </div>
        <SheetFooter className="border-t sm:flex-row sm:justify-end">
          <Button type="button" variant="outline" onClick={onCancel}>
            {t("auth.reauth.cancel")}
          </Button>
          {method && (
            <Button
              type="submit"
              form={`${id}-form`}
              disabled={confirm.isPending || (textInput && !value.trim())}
            >
              {submitLabel}
            </Button>
          )}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
