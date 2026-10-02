import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Notice } from "@/components/admin/notice";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** Confirmation for changes that can lock people out; shows a warning and the server's answer. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  warning,
  error,
  confirmLabel,
  cancelLabel,
  pending = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  warning?: ReactNode;
  error?: ReactNode;
  confirmLabel: string;
  cancelLabel: string;
  pending?: boolean;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent closeLabel={t("common.close")}>
        <DialogHeader>
          <DialogTitle className="text-base">{title}</DialogTitle>
          {description && <DialogDescription className="text-ui">{description}</DialogDescription>}
        </DialogHeader>
        {warning && <Notice tone="warning">{warning}</Notice>}
        {error && <Notice tone="error">{error}</Notice>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">{cancelLabel}</Button>
          </DialogClose>
          <Button variant="destructive" disabled={pending || Boolean(error)} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
