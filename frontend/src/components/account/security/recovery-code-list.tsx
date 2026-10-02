import { Check, Copy, Download } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";

/** Freshly generated recovery codes with copy and download (shown only once). */
export function RecoveryCodeList({ codes }: { codes: string[] }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const text = `${codes.join("\n")}\n`;

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard blocked (insecure context): the codes can still be selected by hand.
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = t("account.security.recovery.fileName");
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex flex-col gap-3">
      <ul
        aria-label={t("account.security.recovery.listLabel")}
        className="grid grid-cols-2 gap-x-6 gap-y-1.5 rounded-lg border bg-muted/40 px-4 py-3 font-mono text-sm"
      >
        {codes.map((code) => (
          <li key={code} className="select-all">
            {code}
          </li>
        ))}
      </ul>
      <div className="flex gap-2">
        <Button type="button" variant="outline" size="sm" className="text-ui" onClick={copy}>
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          <span aria-live="polite">
            {copied
              ? t("account.security.recovery.copied")
              : t("account.security.recovery.copyAll")}
          </span>
        </Button>
        <Button type="button" variant="outline" size="sm" className="text-ui" onClick={download}>
          <Download aria-hidden />
          {t("account.security.recovery.download")}
        </Button>
      </div>
    </div>
  );
}
