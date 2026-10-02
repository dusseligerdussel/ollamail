import { Check, Copy } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Read-only value (redirect URI, invitation link) with a copy button. */
export function CopyField({
  label,
  value,
  description,
}: {
  label: string;
  value: string;
  description?: string;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      // Clipboard blocked (insecure context): select the text so it can be copied by hand.
      const input = document.getElementById(id);
      if (input instanceof HTMLInputElement) input.select();
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={id} className="text-ui">
        {label}
      </Label>
      <div className="flex gap-2">
        <Input id={id} readOnly value={value} className="font-mono text-xs md:text-xs" />
        <Button
          type="button"
          variant="outline"
          className="shrink-0"
          onClick={copy}
          aria-label={t("copy.label", { label })}
        >
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          <span aria-live="polite">{copied ? t("copy.copied") : t("copy.copy")}</span>
        </Button>
      </div>
      {description && <p className="text-xs text-muted-foreground">{description}</p>}
    </div>
  );
}
