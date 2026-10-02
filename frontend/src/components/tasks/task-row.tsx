import { Link } from "@tanstack/react-router";
import { Mail, X } from "lucide-react";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Todo } from "@/api/todos";
import { ExportTaskButton, TaskExportIndicator } from "@/components/task-export/task-export-status";
import { DueDatePicker } from "@/components/tasks/due-date-picker";
import { isOptimistic, useTodoMutations } from "@/components/tasks/use-todo-mutations";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface TaskRowProps {
  todo: Todo;
  today: string;
  /** Keyboard focus of the list (`j`/`k`). */
  active?: boolean;
  onActivate?: () => void;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  dateOpen: boolean;
  onDateOpenChange: (open: boolean) => void;
  /** Link to the source mail (not inside the mail itself). */
  showSource?: boolean;
}

/**
 * One task: check box, title (click to edit), source mail, due date, dismiss. Changes are
 * optimistic, see `useTodoMutations`.
 */
export function TaskRow({
  todo,
  today,
  active = false,
  onActivate,
  editing,
  onEditingChange,
  dateOpen,
  onDateOpenChange,
  showSource = true,
}: TaskRowProps) {
  const { t } = useTranslation();
  const { toggleDone, setDueDate, rename, dismiss } = useTodoMutations();
  const done = todo.status === "done";
  const pending = isOptimistic(todo);
  const titleRef = useRef<HTMLButtonElement>(null);
  const wasEditing = useRef(false);

  // Back to the title after editing, so the keyboard flow continues where it was.
  useEffect(() => {
    if (wasEditing.current && !editing) titleRef.current?.focus({ preventScroll: true });
    wasEditing.current = editing;
  }, [editing]);

  const datePicker = (
    <DueDatePicker
      value={todo.due_date}
      today={today}
      done={done}
      open={dateOpen}
      onOpenChange={onDateOpenChange}
      onChange={(dueDate) => setDueDate(todo, dueDate)}
      className="sm:justify-self-end sm:[grid-area:date]"
    />
  );
  const mailLink = showSource && todo.message_id && (
    <Button
      asChild
      variant="ghost"
      size="icon-sm"
      className="size-7 text-muted-foreground sm:size-8 sm:[grid-area:mail]"
    >
      <Link
        to="/inbox"
        search={{ message: todo.message_id }}
        aria-label={t("tasks.openMail", { title: todo.title })}
        title={t("tasks.openMailShort")}
      >
        <Mail aria-hidden="true" />
      </Link>
    </Button>
  );

  // A grid with fixed columns, so date and mail line up across rows. Narrow screens move date
  // and mail below the title to leave the title its width.
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: only follows the focus of the controls inside, so `j`/`k` continue from there
    <div
      data-active={active || undefined}
      data-done={done || undefined}
      onFocus={onActivate}
      className={cn(
        "group grid min-h-row grid-cols-[auto_minmax(0,1fr)_auto_2rem] items-center gap-x-2 border-b border-border/60 px-4 text-ui md:px-5",
        "[grid-template-areas:'check_title_setdate_dismiss'_'._meta_meta_meta']",
        "sm:grid-cols-[auto_minmax(0,1fr)_8rem_2rem_2rem] sm:gap-x-3 sm:[grid-template-areas:'check_title_date_mail_dismiss']",
        "hover:bg-accent/40 data-active:bg-accent/60 data-active:shadow-[inset_2px_0_0_var(--ring)]",
      )}
    >
      <Checkbox
        checked={done}
        disabled={pending}
        onCheckedChange={() => toggleDone(todo)}
        aria-label={
          done
            ? t("tasks.reopenTask", { title: todo.title })
            : t("tasks.completeTask", { title: todo.title })
        }
        className="mr-1 rounded-full [grid-area:check]"
      />
      <div className="flex min-w-0 flex-col py-1 [grid-area:title]">
        {editing ? (
          <TitleInput
            title={todo.title}
            onDone={(title) => {
              if (title !== null) rename(todo, title);
              onEditingChange(false);
            }}
          />
        ) : (
          <div className="flex min-w-0 items-center gap-1.5">
            <button
              ref={titleRef}
              type="button"
              data-task-title
              disabled={pending}
              onClick={() => onEditingChange(true)}
              title={t("tasks.rename")}
              className={cn(
                "-mx-1 min-w-0 truncate rounded-sm px-1 py-0.5 text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/80",
                done && "text-muted-foreground line-through decoration-muted-foreground/60",
              )}
            >
              {todo.title}
            </button>
            <TaskExportIndicator todo={todo} />
            {!pending && (
              <ExportTaskButton
                todo={todo}
                className="-my-1 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 group-data-active:opacity-100 pointer-coarse:opacity-100 max-sm:opacity-100"
              />
            )}
          </div>
        )}
        {todo.description && !editing && (
          <span className="truncate text-xs text-muted-foreground">{todo.description}</span>
        )}
      </div>
      {todo.due_date || mailLink ? (
        <div className="-mt-1 mb-1 -ml-2 flex items-center gap-1 [grid-area:meta] sm:contents">
          {todo.due_date && datePicker}
          {mailLink}
        </div>
      ) : null}
      {!todo.due_date && <div className="[grid-area:setdate] sm:contents">{datePicker}</div>}
      <Button
        variant="ghost"
        size="icon-sm"
        disabled={pending}
        onClick={() => dismiss(todo)}
        aria-label={t("tasks.dismissTask", { title: todo.title })}
        title={t("tasks.dismiss")}
        className="text-muted-foreground opacity-0 [grid-area:dismiss] group-focus-within:opacity-100 group-hover:opacity-100 group-data-active:opacity-100 pointer-coarse:opacity-100 max-sm:opacity-100"
      >
        <X aria-hidden="true" />
      </Button>
    </div>
  );
}

/** Edits a title in place: Enter or leaving the field saves, Escape cancels. */
function TitleInput({ title, onDone }: { title: string; onDone: (title: string | null) => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState(title);
  const finished = useRef(false);

  const finish = (result: string | null) => {
    if (finished.current) return;
    finished.current = true;
    onDone(result);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(null);
    }
  };

  return (
    <Input
      autoFocus
      aria-label={t("tasks.title")}
      value={value}
      maxLength={255}
      onChange={(event) => setValue(event.target.value)}
      onKeyDown={onKeyDown}
      onBlur={() => finish(value)}
      onFocus={(event) => event.currentTarget.select()}
      className="-mx-2 h-7 px-2 text-ui md:text-ui"
    />
  );
}
