import { Plus } from "lucide-react";
import { type FormEvent, type Ref, useState } from "react";
import { useTranslation } from "react-i18next";

import { useTodoMutations } from "@/components/tasks/use-todo-mutations";
import { cn } from "@/lib/utils";

interface NewTaskInputProps {
  /** Links the new task to this mail. */
  messageId?: string;
  placeholder: string;
  ref?: Ref<HTMLInputElement>;
  autoFocus?: boolean;
  /** Escape or leaving the empty field. */
  onCancel?: () => void;
  className?: string;
}

/** Creates a task from its title (Enter); the field stays focused for the next one. */
export function NewTaskInput({
  messageId,
  placeholder,
  ref,
  autoFocus,
  onCancel,
  className,
}: NewTaskInputProps) {
  const { t } = useTranslation();
  const { create } = useTodoMutations();
  const [title, setTitle] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = title.trim();
    if (!trimmed) return;
    create({ title: trimmed, ...(messageId && { message_id: messageId }) });
    setTitle("");
  };

  return (
    <form
      onSubmit={submit}
      className={cn(
        "flex min-h-row items-center gap-3 border-b border-border/60 px-4 md:px-5",
        "focus-within:bg-accent/40 focus-within:shadow-[inset_2px_0_0_var(--ring)]",
        className,
      )}
    >
      <Plus aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
      <input
        ref={ref}
        // biome-ignore lint/a11y/noAutofocus: shown because the user asked to add a task
        autoFocus={autoFocus}
        aria-label={t("tasks.newTask")}
        placeholder={placeholder}
        value={title}
        maxLength={255}
        onChange={(event) => setTitle(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            setTitle("");
            event.currentTarget.blur();
            onCancel?.();
          }
        }}
        onBlur={() => {
          if (!title.trim()) onCancel?.();
        }}
        className="h-9 min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground"
      />
    </form>
  );
}
