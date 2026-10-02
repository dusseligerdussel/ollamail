import { ArrowDown, ArrowUp, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

/** Moves the item at `index` by `offset` (-1 up, +1 down). */
export function moved<T>(items: readonly T[], index: number, offset: -1 | 1): T[] {
  const target = index + offset;
  if (target < 0 || target >= items.length) return [...items];
  const next = [...items];
  [next[index], next[target]] = [next[target] as T, next[index] as T];
  return next;
}

/** One category in a settings list: name, description, order buttons and actions. */
export function CategoryRow({
  name,
  description,
  tag,
  muted,
  first,
  last,
  disabled,
  onMove,
  onEdit,
  onDelete,
  children,
}: {
  name: string;
  description: string;
  /** Short note next to the name, e.g. "Organization". */
  tag?: string;
  /** Hidden categories are shown dimmed. */
  muted?: boolean;
  first: boolean;
  last: boolean;
  disabled?: boolean;
  onMove: (offset: -1 | 1) => void;
  onEdit?: () => void;
  onDelete?: () => void;
  /** Further controls, e.g. the visibility switch. */
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <div className={cn("min-w-0 flex-1", muted && "opacity-60")}>
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="truncate text-ui font-medium">{name}</span>
          {tag && <span className="shrink-0 text-xs text-muted-foreground">{tag}</span>}
        </div>
        <p className="line-clamp-2 text-ui text-muted-foreground">
          {description || t("triage.settings.noDescription")}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={first || disabled}
          onClick={() => onMove(-1)}
          aria-label={t("triage.settings.moveUp", { category: name })}
          title={t("triage.settings.moveUp", { category: name })}
        >
          <ArrowUp />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={last || disabled}
          onClick={() => onMove(1)}
          aria-label={t("triage.settings.moveDown", { category: name })}
          title={t("triage.settings.moveDown", { category: name })}
        >
          <ArrowDown />
        </Button>
        {children}
        {/* Keeps the controls of rows without actions aligned with the others. */}
        {!onEdit && !onDelete && <span aria-hidden="true" className="size-8" />}
        {(onEdit || onDelete) && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={t("triage.settings.actions", { category: name })}
              >
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {onEdit && (
                <DropdownMenuItem onSelect={onEdit}>
                  <Pencil />
                  {t("triage.settings.edit")}
                </DropdownMenuItem>
              )}
              {onEdit && onDelete && <DropdownMenuSeparator />}
              {onDelete && (
                <DropdownMenuItem onSelect={onDelete}>
                  <Trash2 />
                  {t("triage.settings.delete")}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </li>
  );
}

export function DeleteCategoryDialog({
  open,
  description,
  pending,
  onClose,
  onConfirm,
}: {
  open: boolean;
  description: string;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("triage.settings.deleteTitle")}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("triage.settings.cancel")}
          </Button>
          <Button variant="destructive" disabled={pending} onClick={onConfirm}>
            {t("triage.settings.delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
