import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { messageTodosQueryOptions } from "@/api/todos";
import { useCommands } from "@/components/command-palette/command-provider";
import { NewTaskInput } from "@/components/tasks/new-task-input";
import { TaskRow } from "@/components/tasks/task-row";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import type { Command } from "@/lib/commands";
import { todayIn } from "@/lib/task-dates";

/**
 * "Tasks from this message" below the thread (#23): the open and done tasks extracted from or
 * linked to the mail, plus adding one by hand. Without tasks only the add button is shown.
 * It registers no single-key shortcuts, so the inbox keys stay with the inbox (#16, #21).
 */
export function MessageTasks({ messageId }: { messageId: string }) {
  const { t } = useTranslation();
  const { timezone } = useCurrentUser();
  const today = todayIn(timezone);
  // Failures stay quiet here: the tasks are secondary to the mail itself.
  const todos = useQuery({ ...messageTodosQueryOptions(messageId), meta: { errorToast: false } });
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [dateId, setDateId] = useState<string | null>(null);

  const commands = useMemo<Command[]>(
    () => [
      {
        id: "tasks.addFromMail",
        label: t("tasks.addFromMail"),
        group: "actions",
        icon: Plus,
        run: () => setAdding(true),
      },
    ],
    [t],
  );
  useCommands(commands);

  const items = todos.data ?? [];
  if (todos.isPending || todos.isError) return null;

  if (items.length === 0 && !adding) {
    return (
      <div>
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground"
          onClick={() => setAdding(true)}
        >
          <Plus aria-hidden="true" />
          {t("tasks.addFromMail")}
        </Button>
      </div>
    );
  }

  return (
    <section aria-labelledby={`message-tasks-${messageId}`} className="rounded-lg border">
      <header className="flex items-center gap-2 border-b border-border/60 px-4 py-2 md:px-5">
        <h2 id={`message-tasks-${messageId}`} className="text-ui font-medium">
          {t("tasks.fromMail")}
        </h2>
        <Link
          to="/tasks"
          className="ml-auto rounded-sm text-xs text-muted-foreground underline-offset-4 outline-none hover:text-foreground hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/80"
        >
          {t("tasks.showAll")}
        </Link>
      </header>
      <ul>
        {items.map((todo) => (
          <li key={todo.id}>
            <TaskRow
              todo={todo}
              today={today}
              showSource={false}
              editing={editingId === todo.id}
              onEditingChange={(editing) => setEditingId(editing ? todo.id : null)}
              dateOpen={dateId === todo.id}
              onDateOpenChange={(open) => setDateId(open ? todo.id : null)}
            />
          </li>
        ))}
      </ul>
      <NewTaskInput
        messageId={messageId}
        placeholder={t("tasks.newTaskPlaceholder")}
        autoFocus={adding}
        onCancel={() => setAdding(false)}
        className="border-b-0"
      />
    </section>
  );
}
