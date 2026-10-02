import { useQuery } from "@tanstack/react-query";
import { CalendarCheck, CalendarX, CircleAlert, Clock, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type ExportTarget, todoExportQueryOptions, useExportTodo } from "@/api/todo-export";
import type { Todo } from "@/api/todos";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { cn } from "@/lib/utils";

/** The connected, admin-allowed export target, if any (failures count as none). */
export function useExportTarget(): ExportTarget | undefined {
  const settings = useQuery({ ...todoExportQueryOptions, meta: { errorToast: false } });
  const target = settings.data?.target;
  return target?.active ? target : undefined;
}

const icons = {
  synced: CalendarCheck,
  pending: Clock,
  error: CircleAlert,
  removed: CalendarX,
} as const;

/** Small icon after the title: exported, waiting, failed or deleted in the target system. */
export function TaskExportIndicator({ todo }: { todo: Todo }) {
  const { t } = useTranslation();
  const state = todo.export_state;
  if (!state) return null;
  const Icon = icons[state.state];
  const label = t(`tasks.export.${state.state}`);
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      data-export-state={state.state}
      className={cn(
        "inline-flex shrink-0",
        state.state === "error" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      <Icon aria-hidden className="size-3.5" />
    </span>
  );
}

/** Whether the user may export this task by hand now (manual mode, or again after a deletion). */
export function canExportByHand(todo: Todo, target: ExportTarget | undefined, userId: string) {
  if (!target || todo.status === "dismissed") return false;
  const mine = !todo.shared || todo.assignee_id === userId;
  if (!mine) return false;
  const state = todo.export_state?.state;
  if (state === "removed") return true;
  return target.mode === "manual" && (state === undefined || state === "error");
}

export function ExportTaskButton({ todo, className }: { todo: Todo; className?: string }) {
  const { t } = useTranslation();
  const target = useExportTarget();
  const { id: userId } = useCurrentUser();
  const exportTodo = useExportTodo();
  if (!canExportByHand(todo, target, userId)) return null;
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      disabled={exportTodo.isPending}
      onClick={() => exportTodo.mutate(todo.id)}
      aria-label={t("tasks.export.exportTask", { title: todo.title, list: target?.list_name })}
      title={t("tasks.export.export", { list: target?.list_name })}
      className={cn("size-7 text-muted-foreground", className)}
    >
      <Upload aria-hidden="true" />
    </Button>
  );
}
