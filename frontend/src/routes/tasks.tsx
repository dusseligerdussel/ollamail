import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { CalendarDays, Check, ListChecks, Mail, Pencil, Plus, RotateCcw, X } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { type Todo, todosQueryOptions } from "@/api/todos";
import { useCommands } from "@/components/command-palette/command-provider";
import { EmptyState } from "@/components/empty-state";
import { InlineError } from "@/components/inline-error";
import { ListSkeleton } from "@/components/list-skeleton";
import { PageHeader } from "@/components/page-header";
import { useShortcut } from "@/components/shortcuts/shortcut-provider";
import { NewTaskInput } from "@/components/tasks/new-task-input";
import { TaskRow } from "@/components/tasks/task-row";
import { isOptimistic, useTodoMutations } from "@/components/tasks/use-todo-mutations";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { useListNavigation } from "@/hooks/use-list-navigation";
import type { Command } from "@/lib/commands";
import { groupTasks, todayIn } from "@/lib/task-dates";

export const Route = createFileRoute("/tasks")({
  component: TasksPage,
});

function TasksPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { timezone } = useCurrentUser();
  const today = todayIn(timezone);
  const open = useQuery(todosQueryOptions("open"));
  const done = useQuery(todosQueryOptions("done"));
  const newTask = useRef<HTMLInputElement>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [dateId, setDateId] = useState<string | null>(null);
  const { toggleDone, dismiss } = useTodoMutations();

  const groups = useMemo(
    () => groupTasks([...(open.data ?? []), ...(done.data ?? [])], today),
    [open.data, done.data, today],
  );
  const items = useMemo(() => groups.flatMap((group) => group.todos), [groups]);

  const openMail = useCallback(
    (todo: Todo | undefined) => {
      if (todo?.message_id) void navigate({ to: "/inbox", search: { message: todo.message_id } });
    },
    [navigate],
  );
  const { activeIndex, setActiveIndex } = useListNavigation({
    count: items.length,
    onOpen: (index) => openMail(items[index]),
  });
  const active = items[activeIndex];
  const usable = active && !isOptimistic(active) ? active : undefined;

  const focusNew = useCallback(() => newTask.current?.focus(), []);
  useShortcut(
    { id: "tasks.new", keys: "n", group: "list", description: t("tasks.shortcuts.new") },
    focusNew,
  );
  useShortcut(
    {
      id: "tasks.toggleDone",
      keys: "x",
      group: "list",
      description: t("tasks.shortcuts.toggleDone"),
      enabled: !!usable,
    },
    () => usable && toggleDone(usable),
  );
  // `e` (done) as everywhere in the app (docs/DESIGN.md); unlike `x` it never reopens a task.
  useShortcut(
    {
      id: "tasks.done",
      keys: "e",
      group: "list",
      description: t("tasks.shortcuts.done"),
      enabled: !!usable && usable.status !== "done",
    },
    () => usable && toggleDone(usable),
  );
  useShortcut(
    {
      id: "tasks.date",
      keys: "d",
      group: "list",
      description: t("tasks.shortcuts.date"),
      enabled: !!usable,
    },
    () => usable && setDateId(usable.id),
  );
  // `j`/`k` move the focus along, so Enter (edit title) and Tab start from the active task.
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const row = list.current?.querySelector(`[data-task-index="${activeIndex}"]`);
    if (row && !row.contains(document.activeElement)) {
      row.querySelector<HTMLElement>("[data-task-title]")?.focus();
    }
  }, [activeIndex]);

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [
      {
        id: "tasks.new",
        label: t("tasks.newTask"),
        group: "actions",
        icon: Plus,
        shortcut: "n",
        run: focusNew,
      },
    ];
    if (usable) {
      const isDone = usable.status === "done";
      list.push(
        {
          id: "tasks.toggleDone",
          label: isDone ? t("tasks.reopen") : t("tasks.complete"),
          group: "actions",
          icon: isDone ? RotateCcw : Check,
          shortcut: "x",
          run: () => toggleDone(usable),
        },
        {
          id: "tasks.date",
          label: t("tasks.changeDate"),
          group: "actions",
          icon: CalendarDays,
          shortcut: "d",
          run: () => setDateId(usable.id),
        },
        {
          id: "tasks.rename",
          label: t("tasks.rename"),
          group: "actions",
          icon: Pencil,
          run: () => setEditingId(usable.id),
        },
        {
          id: "tasks.dismiss",
          label: t("tasks.dismiss"),
          group: "actions",
          icon: X,
          run: () => dismiss(usable),
        },
      );
      if (usable.message_id) {
        list.push({
          id: "tasks.openMail",
          label: t("tasks.openMailShort"),
          group: "actions",
          icon: Mail,
          shortcut: "o",
          run: () => openMail(usable),
        });
      }
    }
    // Found when searching for "task" as well.
    return list.map((command) => ({ ...command, keywords: [t("nav.tasks")] }));
  }, [t, usable, toggleDone, dismiss, focusNew, openMail]);
  useCommands(commands);

  const openCount = open.data?.length ?? 0;
  let content: ReactNode;
  if (open.isPending || done.isPending) {
    content = <ListSkeleton rows={10} />;
  } else if (open.isError || done.isError) {
    content = <InlineError error={open.error ?? done.error} className="m-4" />;
  } else if (items.length === 0) {
    content = (
      <EmptyState
        icon={ListChecks}
        title={t("pages.tasks.emptyTitle")}
        description={t("pages.tasks.emptyDescription")}
        action={
          <Button asChild size="sm" variant="outline">
            <Link to="/inbox">{t("pages.tasks.emptyAction")}</Link>
          </Button>
        }
      />
    );
  } else {
    let index = 0;
    content = groups.map(({ group, todos }) => (
      <section key={group} aria-labelledby={`tasks-${group}`}>
        <h2
          id={`tasks-${group}`}
          className="sticky top-0 z-10 flex items-baseline gap-2 border-b border-border/60 bg-background px-4 pt-5 pb-1.5 text-xs font-medium text-muted-foreground md:px-5"
        >
          <span className={group === "overdue" ? "text-destructive" : undefined}>
            {t(`tasks.groups.${group}`)}
          </span>
          <span className="font-normal tabular-nums">{todos.length}</span>
        </h2>
        <ul>
          {todos.map((todo) => {
            const position = index++;
            return (
              <li key={todo.id} data-task-index={position}>
                <TaskRow
                  todo={todo}
                  today={today}
                  active={position === activeIndex}
                  onActivate={() => setActiveIndex(position)}
                  editing={editingId === todo.id}
                  onEditingChange={(editing) => setEditingId(editing ? todo.id : null)}
                  dateOpen={dateId === todo.id}
                  onDateOpenChange={(isOpen) => setDateId(isOpen ? todo.id : null)}
                />
              </li>
            );
          })}
        </ul>
      </section>
    ));
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={t("nav.tasks")}
        meta={openCount > 0 ? new Intl.NumberFormat().format(openCount) : undefined}
      />
      <div ref={list} className="flex-1 overflow-y-auto pb-8" data-testid="task-list">
        <div className="mx-auto w-full max-w-3xl">
          <NewTaskInput ref={newTask} placeholder={t("tasks.newTaskPlaceholder")} />
          {content}
        </div>
      </div>
    </div>
  );
}
