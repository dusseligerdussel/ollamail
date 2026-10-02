import { useMutation } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { type FormEvent, useCallback, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { pageNavigation } from "@/api/auth";
import { problemErrorCode } from "@/api/mail";
import {
  type ExportMode,
  type ExportSink,
  type ExportTarget,
  listTaskLists,
  startGoogleTasksOAuth,
  type TaskList,
  useSaveTodoExport,
} from "@/api/todo-export";
import { FormError, FormField } from "@/components/form-field";
import { MsTodoConnect } from "@/components/task-export/mstodo-connect";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";

/** Error codes of the export API with their own message (`taskExport.errors.*`). */
const KNOWN_ERRORS = new Set([
  "auth_failed",
  "unavailable",
  "timeout",
  "not_found",
  "invalid_url",
  "insecure_url",
  "sink_not_available",
  "unknown_list",
  // Microsoft To Do sign-in (`mstodo=error&reason=…`) and its API.
  "not_configured",
  "mstodo_not_connected",
  "consent_denied",
  "access_denied",
  "state_invalid",
  "session_mismatch",
  "token_exchange_failed",
  "offline_access_missing",
  "list_not_found",
  // Google Tasks sign-in (`?gtasks_error=` after the redirect back from Google).
  "oauth_required",
  "oauth_not_configured",
  "insufficient_scope",
  "invalid_state",
  "token_revoked",
  "refresh_token_missing",
  "no_lists",
  "api_disabled",
]);

export function useExportErrorText() {
  const { t } = useTranslation();
  return useCallback(
    (code: string | null | undefined, sink?: ExportSink) => {
      if (sink === "mstodo" && code === "auth_failed")
        return t("taskExport.errors.mstodo_auth_failed");
      // Google has no user name or password; the user connects again.
      if (sink === "gtasks" && code === "auth_failed") return t("taskExport.errors.reconnect");
      return code && KNOWN_ERRORS.has(code)
        ? t(`taskExport.errors.${code as "auth_failed"}`)
        : t("taskExport.errors.other");
    },
    [t],
  );
}

/** What leaves the instance, shown before the user connects (docs/PRIVACY.md). */
export function ExportPrivacyNotice({ sink }: { sink?: ExportSink }) {
  const { t } = useTranslation();
  if (sink === "gtasks") {
    return (
      <div className="flex items-start gap-3 rounded-lg border bg-muted/40 px-4 py-3 text-ui">
        <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="flex flex-col gap-1">
          <p>{t("taskExport.privacy.sentGoogle")}</p>
          <p className="text-muted-foreground">{t("taskExport.privacy.notSentGoogle")}</p>
        </div>
      </div>
    );
  }
  if (sink === "mstodo") {
    return (
      <div className="flex items-start gap-3 rounded-lg border bg-muted/40 px-4 py-3 text-ui">
        <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="flex flex-col gap-1">
          <p>{t("taskExport.mstodo.privacySent")}</p>
          <p className="text-muted-foreground">{t("taskExport.mstodo.privacyNotSent")}</p>
        </div>
      </div>
    );
  }
  return (
    <div className="flex items-start gap-3 rounded-lg border bg-muted/40 px-4 py-3 text-ui">
      <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="flex flex-col gap-1">
        <p>{t("taskExport.privacy.sent")}</p>
        <p className="text-muted-foreground">{t("taskExport.privacy.notSent")}</p>
      </div>
    </div>
  );
}

export function ModeChoice({
  value,
  onChange,
  disabled,
}: {
  value: ExportMode;
  onChange: (mode: ExportMode) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <RadioGroup
      aria-label={t("taskExport.mode.label")}
      value={value}
      disabled={disabled}
      onValueChange={(next) => onChange(next as ExportMode)}
      className="gap-0 divide-y rounded-lg border"
    >
      {(["auto", "manual"] as const).map((mode) => (
        <div key={mode} className="flex items-start gap-3 px-4 py-3">
          <RadioGroupItem id={`${id}-${mode}`} value={mode} className="mt-0.5" />
          <Label
            htmlFor={`${id}-${mode}`}
            className="flex min-w-0 flex-1 flex-col items-start gap-0.5"
          >
            <span className="text-ui font-medium">{t(`taskExport.mode.${mode}`)}</span>
            <span className="text-ui font-normal text-muted-foreground">
              {t(`taskExport.mode.${mode}Description`)}
            </span>
          </Label>
        </div>
      ))}
    </RadioGroup>
  );
}

/**
 * Google Tasks: sign in with Google; the mode is chosen here, the list after the redirect back
 * (the default list is used until then). Nothing is stored before the provider agreed.
 */
function GoogleTasksConnect({
  current,
  onCancel,
}: {
  current?: ExportTarget;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();
  const errorText = useExportErrorText();
  const [mode, setMode] = useState<ExportMode>(current?.mode ?? "auto");
  const start = useMutation({
    mutationFn: () => startGoogleTasksOAuth(mode),
    meta: { errorToast: false },
    onSuccess: ({ authorization_url }) => pageNavigation.assign(authorization_url),
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    start.mutate();
  };

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-6" noValidate>
      <p className="text-ui text-muted-foreground">{t("taskExport.google.hint")}</p>
      {!current && (
        <div className="flex flex-col gap-2">
          <div className="text-ui">{t("taskExport.mode.label")}</div>
          <ModeChoice value={mode} onChange={setMode} />
        </div>
      )}
      {start.isError && <FormError>{errorText(problemErrorCode(start.error))}</FormError>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={start.isPending || start.isSuccess}>
          {start.isPending || start.isSuccess
            ? t("taskExport.google.redirecting")
            : t(current ? "taskExport.google.reconnect" : "taskExport.google.connect")}
        </Button>
        {onCancel && (
          <Button type="button" size="sm" variant="outline" onClick={onCancel}>
            {t("taskExport.cancel")}
          </Button>
        )}
      </div>
    </form>
  );
}

interface ConnectFormProps {
  sinks: ExportSink[];
  /** Change an existing connection (server, account, list). */
  current?: ExportTarget;
  /** Target the user just signed in to (OAuth flow), preselected. */
  signedIn?: ExportSink;
  /** Preselected target, e.g. after a failed OAuth sign-in. */
  initialSink?: ExportSink;
  onDone: () => void;
  onCancel?: () => void;
}

/**
 * Connect the export in two steps: target and credentials (checked by listing the task lists),
 * then list and mode. Nothing is stored before the second step.
 */
export function ConnectForm({
  sinks,
  current,
  signedIn,
  initialSink,
  onDone,
  onCancel,
}: ConnectFormProps) {
  const { t } = useTranslation();
  const errorText = useExportErrorText();
  const sinkLabel = useId();
  const [sink, setSink] = useState<ExportSink>(
    signedIn ??
      current?.sink ??
      (initialSink && sinks.includes(initialSink) ? initialSink : sinks[0]) ??
      "caldav",
  );
  // Targets connected by signing in have no URL or password.
  const credentials = sink !== "mstodo" && sink !== "gtasks";
  const [url, setUrl] = useState(current?.url ?? "");
  const [username, setUsername] = useState(current?.username ?? "");
  const [password, setPassword] = useState("");
  const [lists, setLists] = useState<TaskList[]>();
  const [listId, setListId] = useState(current?.list_id ?? "");
  const [mode, setMode] = useState<ExportMode>(current?.mode ?? "auto");
  const save = useSaveTodoExport();

  // An unchanged account keeps the stored password when the field stays empty.
  const keepsPassword =
    !!current?.has_password &&
    current.sink === sink &&
    current.url === url.trim() &&
    current.username === username.trim();
  const connection = () => ({
    sink,
    url: url.trim(),
    username: username.trim(),
    password: password || (keepsPassword ? null : ""),
  });

  const discover = useMutation({
    mutationFn: () => listTaskLists(connection()),
    meta: { errorToast: false },
    onSuccess: (found) => {
      setLists(found);
      setListId((selected) =>
        found.some((list) => list.id === selected) ? selected : (found[0]?.id ?? ""),
      );
    },
  });

  const resetLists = () => {
    setLists(undefined);
    discover.reset();
  };

  const onConnect = (event: FormEvent) => {
    event.preventDefault();
    discover.mutate();
  };

  const onSave = (event: FormEvent) => {
    event.preventDefault();
    save.mutate({ ...connection(), list_id: listId, mode }, { onSuccess: onDone });
  };

  const discoverError = discover.isError ? errorText(problemErrorCode(discover.error)) : undefined;

  return (
    <div className="flex flex-col gap-8">
      <ExportPrivacyNotice sink={sink} />
      <form onSubmit={onConnect} className="flex flex-col gap-4" noValidate>
        {sinks.length > 0 && (
          <section aria-labelledby={sinkLabel}>
            <h2 id={sinkLabel} className="mb-2 text-xs font-medium text-muted-foreground">
              {t("taskExport.target")}
            </h2>
            <RadioGroup
              aria-labelledby={sinkLabel}
              value={sink}
              onValueChange={(next) => {
                setSink(next as ExportSink);
                resetLists();
              }}
              className="gap-0 divide-y rounded-lg border"
            >
              {sinks.map((kind) => (
                <div key={kind} className="flex items-start gap-3 px-4 py-3">
                  <RadioGroupItem id={`${sinkLabel}-${kind}`} value={kind} className="mt-0.5" />
                  <Label
                    htmlFor={`${sinkLabel}-${kind}`}
                    className="flex min-w-0 flex-1 flex-col items-start gap-0.5"
                  >
                    <span className="text-ui font-medium">
                      {t(`taskExport.sinks.${kind}.title`)}
                    </span>
                    <span className="text-ui font-normal text-muted-foreground">
                      {t(`taskExport.sinks.${kind}.description`)}
                    </span>
                  </Label>
                </div>
              ))}
            </RadioGroup>
          </section>
        )}
        {credentials && (
          <>
            <FormField
              label={t("taskExport.url")}
              description={t("taskExport.urlHint")}
              type="url"
              inputMode="url"
              autoComplete="url"
              placeholder="https://cloud.example.org/remote.php/dav"
              value={url}
              required
              onChange={(event) => {
                setUrl(event.target.value);
                resetLists();
              }}
            />
            <FormField
              label={t("taskExport.username")}
              autoComplete="username"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                resetLists();
              }}
            />
            <FormField
              label={t("taskExport.password")}
              description={
                keepsPassword ? t("taskExport.passwordKept") : t("taskExport.passwordHint")
              }
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                resetLists();
              }}
            />
            {discoverError && <FormError>{discoverError}</FormError>}
            {!lists && (
              <div className="flex gap-2">
                <Button type="submit" size="sm" disabled={!url.trim() || discover.isPending}>
                  {discover.isPending ? t("taskExport.connecting") : t("taskExport.connect")}
                </Button>
                {onCancel && (
                  <Button type="button" size="sm" variant="outline" onClick={onCancel}>
                    {t("taskExport.cancel")}
                  </Button>
                )}
              </div>
            )}
          </>
        )}
      </form>
      {sink === "mstodo" && (
        <MsTodoConnect
          current={current}
          signedIn={signedIn === "mstodo"}
          onDone={onDone}
          onCancel={onCancel}
        />
      )}
      {sink === "gtasks" && (
        <GoogleTasksConnect
          current={current?.sink === sink ? current : undefined}
          onCancel={onCancel}
        />
      )}
      {credentials && lists && (
        <form onSubmit={onSave} className="flex flex-col gap-6">
          {lists.length === 0 ? (
            <FormError>{t("taskExport.noLists")}</FormError>
          ) : (
            <div className="flex flex-col gap-2">
              <Label htmlFor="task-export-list" className="text-ui">
                {t("taskExport.list")}
              </Label>
              <NativeSelect
                id="task-export-list"
                className="w-full"
                value={listId}
                onChange={(event) => setListId(event.target.value)}
              >
                {lists.map((list) => (
                  <NativeSelectOption key={list.id} value={list.id}>
                    {list.name}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </div>
          )}
          <div className="flex flex-col gap-2">
            <div className="text-ui">{t("taskExport.mode.label")}</div>
            <ModeChoice value={mode} onChange={setMode} />
          </div>
          {save.isError && <FormError>{errorText(problemErrorCode(save.error))}</FormError>}
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={!listId || save.isPending}>
              {current ? t("taskExport.save") : t("taskExport.enable")}
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={onCancel ?? resetLists}>
              {t("taskExport.cancel")}
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
