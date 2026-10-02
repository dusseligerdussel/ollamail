import { Check, Copy, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import { type FeedStatus, useCreateFeed, useRevokeFeed } from "@/api/digest";
import { QrCode } from "@/components/digest/qr-code";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCurrentUser } from "@/hooks/use-current-user";
import { formatDateTime } from "@/lib/mail-format";

type Confirm = "rotate" | "revoke" | null;

/**
 * Private podcast feed: create, copy, show as QR code, replace or turn off. The server keeps
 * only a hash of the token, so the URL is shown once, right after it was created.
 */
export function FeedSection({ feed }: { feed: FeedStatus }) {
  const { t, i18n } = useTranslation();
  const { timezone } = useCurrentUser();
  const create = useCreateFeed();
  const revoke = useRevokeFeed();
  const [url, setUrl] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [copied, setCopied] = useState(false);
  const inputId = useId();
  const warningId = useId();

  const createFeed = () =>
    create.mutate(undefined, {
      onSuccess: (created) => {
        setUrl(created.feed_url);
        setConfirm(null);
        setCopied(false);
      },
    });
  const revokeFeed = () =>
    revoke.mutate(undefined, {
      onSuccess: () => {
        setUrl(null);
        setConfirm(null);
      },
    });
  const copy = async () => {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      toast.success(t("digest.feed.copied"));
    } catch {
      // Clipboard blocked: select the text so it can be copied by hand.
      const input = document.getElementById(inputId);
      if (input instanceof HTMLInputElement) input.select();
    }
  };

  const busy = create.isPending || revoke.isPending;
  const since = feed.created_at && formatDateTime(feed.created_at, i18n.language, timezone);

  return (
    <div className="flex flex-col gap-4 px-4 py-3.5">
      <div className="min-w-0">
        <div className="text-ui font-medium">{t("digest.feed.title")}</div>
        <div className="text-ui text-muted-foreground">{t("digest.feed.description")}</div>
      </div>

      <p
        id={warningId}
        className="flex items-start gap-2 rounded-md border px-3 py-2 text-ui text-muted-foreground"
      >
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-foreground" />
        <span>{t("digest.feed.warning")}</span>
      </p>

      {url ? (
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
          <QrCode value={url} label={t("digest.feed.qrLabel")} />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <label htmlFor={inputId} className="text-ui font-medium">
              {t("digest.feed.url")}
            </label>
            <div className="flex gap-2">
              <Input
                id={inputId}
                readOnly
                value={url}
                aria-describedby={warningId}
                onFocus={(event) => event.currentTarget.select()}
                className="min-w-0 flex-1 font-mono text-xs"
              />
              <Button variant="outline" size="sm" className="h-9 shrink-0 text-ui" onClick={copy}>
                {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                {t("digest.feed.copy")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("digest.feed.shownOnce")}</p>
          </div>
        </div>
      ) : feed.active ? (
        <p className="text-ui text-muted-foreground">
          {since ? t("digest.feed.activeSince", { date: since }) : t("digest.feed.active")}{" "}
          {t("digest.feed.urlHidden")}
        </p>
      ) : null}

      {confirm ? (
        <div
          role="alert"
          className="flex flex-col gap-3 rounded-md border border-destructive/40 p-3"
        >
          <p className="text-ui">
            {confirm === "rotate" ? t("digest.feed.rotateWarning") : t("digest.feed.revokeWarning")}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="destructive"
              disabled={busy}
              onClick={confirm === "rotate" ? createFeed : revokeFeed}
            >
              {confirm === "rotate"
                ? t("digest.feed.rotateConfirm")
                : t("digest.feed.revokeConfirm")}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirm(null)}>
              {t("digest.feed.cancel")}
            </Button>
          </div>
        </div>
      ) : feed.active ? (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirm("rotate")}>
            {t("digest.feed.rotate")}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirm("revoke")}>
            {t("digest.feed.revoke")}
          </Button>
        </div>
      ) : (
        <div>
          <Button size="sm" disabled={busy} onClick={createFeed}>
            {t("digest.feed.create")}
          </Button>
        </div>
      )}
    </div>
  );
}
