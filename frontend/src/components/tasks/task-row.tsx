import { Link } from "@tanstack/react-router";
import { Mail, X } from "lucide-react";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Todo } from "@/api/todos";
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

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: only follows the focus of the controls inside, so `j`/`k` continue from there
    <div
      data-active={active || undefined}
      data-done={done || undefined}
      onFocus={onActivate}
      className={cn(
        "group flex min-h-row items-center gap-3 border-b border-border/60 px-4 text-ui md:px-5",
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
        className="rounded-full"
      />
      <div className="flex min-w-0 flex-1 flex-col py-1.5">
        {editing ? (
          <TitleInput
            title={todo.title}
            onDone={(title) => {
              if (title !== null) rename(todo, title);
              onEditingChange(false);
            }}
          />
        ) : (
          <button
            ref={titleRef}
            type="button"
            data-task-title
            disabled={pending}
            onClick={() => onEditingChange(true)}
            title={t("tasks.rename")}
            className={cn(
              "-mx-1 truncate rounded-sm px-1 text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              done && "text-muted-foreground line-through decoration-muted-foreground/60",
            )}
          >
            {todo.title}
          </button>
        )}
        {todo.description && !editing && (
          <span className="truncate text-xs text-muted-foreground">{todo.description}</span>
        )}
      </div>
      {showSource && todo.message_id && (
        <Button asChild variant="ghost" size="icon-sm" className="shrink-0 text-muted-foreground">
          <Link
            to="/inbox"
            search={{ message: todo.message_id }}
            aria-label={t("tasks.openMail", { title: todo.title })}
            title={t("tasks.openMailShort")}
          >
            <Mail aria-hidden="true" />
          </Link>
        </Button>
      )}
      <DueDatePicker
        value={todo.due_date}
        today={today}
        done={done}
        open={dateOpen}
        onOpenChange={onDateOpenChange}
        onChange={(dueDate) => setDueDate(todo, dueDate)}
      />
      <Button
        variant="ghost"
        size="icon-sm"
        disabled={pending}
        onClick={() => dismiss(todo)}
        aria-label={t("tasks.dismissTask", { title: todo.title })}
        title={t("tasks.dismiss")}
        className="shrink-0 text-muted-foreground opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 group-data-active:opacity-100 pointer-coarse:opacity-100"
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
