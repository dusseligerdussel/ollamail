import { Paperclip } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** Chunk sources of an attachment (`ChunkSource` in the backend); `attachment_ocr` is
 * text recognised from a scan. */
export function isAttachmentSource(source: string): boolean {
  return source === "attachment" || source === "attachment_ocr";
}

/** File name of the attachment a hit or source comes from, marked "(OCR)" for scans. */
export function AttachmentSource({
  source,
  filename,
  className,
}: {
  source: string;
  filename: string | null | undefined;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <span className={cn("flex items-center gap-1 text-xs text-muted-foreground", className)}>
      <Paperclip aria-hidden className="size-3 shrink-0" />
      <span className="truncate">{filename || t("mail.unnamedAttachment")}</span>
      {source === "attachment_ocr" && (
        <span className="shrink-0" title={t("search.ocrHint")}>
          {t("search.ocr")}
        </span>
      )}
    </span>
  );
}
