import {
  type QueryClient,
  type QueryKey,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

import {
  createTodo,
  type Todo,
  type TodoCreate,
  type TodoUpdate,
  todoKeys,
  updateTodo,
} from "@/api/todos";

/** Every cached todo list: the task page (`["todo", "list", status]`) and mails. */
const listFilters: { queryKey: QueryKey }[] = [
  { queryKey: todoKeys.all },
  { queryKey: ["message", "todos"] },
];

type Snapshot = [QueryKey, Todo[] | undefined][];

async function snapshot(queryClient: QueryClient): Promise<Snapshot> {
  await Promise.all(listFilters.map((filter) => queryClient.cancelQueries(filter)));
  return listFilters.flatMap((filter) => queryClient.getQueriesData<Todo[]>(filter));
}

function restore(queryClient: QueryClient, saved: Snapshot | undefined) {
  for (const [queryKey, data] of saved ?? []) queryClient.setQueryData(queryKey, data);
}

function refresh(queryClient: QueryClient) {
  for (const filter of listFilters) void queryClient.invalidateQueries(filter);
}

/** Does a cached list (by its key) hold todos of this status? Mail lists hold open and done. */
function listAccepts(queryKey: QueryKey, todo: Todo) {
  if (queryKey[0] === "todo") return queryKey[2] === todo.status;
  return todo.status !== "dismissed" && queryKey[2] === todo.message_id;
}

/** Puts `todo` into every cached list it belongs to and removes it from the others. */
function place(queryClient: QueryClient, todo: Todo) {
  for (const filter of listFilters) {
    for (const [queryKey, data] of queryClient.getQueriesData<Todo[]>(filter)) {
      if (!data) continue;
      const index = data.findIndex((item) => item.id === todo.id);
      const accepts = listAccepts(queryKey, todo);
      let next = data;
      if (index >= 0) {
        next = accepts ? data.with(index, todo) : data.toSpliced(index, 1);
      } else if (accepts) {
        next = [todo, ...data];
      }
      if (next !== data) queryClient.setQueryData(queryKey, next);
    }
  }
}

function applyUpdate(todo: Todo, patch: TodoUpdate): Todo {
  const now = new Date().toISOString();
  const statusChanged = patch.status !== undefined && patch.status !== todo.status;
  return {
    ...todo,
    ...(patch.title != null && { title: patch.title }),
    ...("due_date" in patch && { due_date: patch.due_date ?? null }),
    ...(statusChanged && {
      status: patch.status ?? todo.status,
      completed_at: patch.status === "done" ? now : null,
      done_suggested: false,
    }),
    updated_at: now,
  };
}

interface UpdateInput {
  todo: Todo;
  patch: TodoUpdate;
}

/**
 * Changes to todos, applied to every cached list at once (optimistic) and rolled back if the
 * request fails (the failure itself is shown as a toast by the query client).
 */
export function useTodoMutations() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const update = useMutation({
    mutationFn: ({ todo, patch }: UpdateInput) => updateTodo(todo.id, patch),
    onMutate: async ({ todo, patch }) => {
      const saved = await snapshot(queryClient);
      place(queryClient, applyUpdate(todo, patch));
      return saved;
    },
    onError: (_error, _input, saved) => restore(queryClient, saved),
    onSettled: () => refresh(queryClient),
  });

  const create = useMutation({
    mutationFn: (body: Omit<TodoCreate, "priority">) => createTodo({ priority: "normal", ...body }),
    onMutate: async (body) => {
      const saved = await snapshot(queryClient);
      const now = new Date().toISOString();
      place(queryClient, {
        id: `optimistic-${crypto.randomUUID()}`,
        title: body.title,
        description: body.description ?? null,
        due_date: body.due_date ?? null,
        priority: "normal",
        status: "open",
        is_manual: true,
        is_edited: false,
        confidence: null,
        done_suggested: false,
        mailbox_id: null,
        message_id: body.message_id ?? null,
        thread_id: null,
        external_refs: {},
        created_at: now,
        updated_at: now,
        completed_at: null,
      });
      return saved;
    },
    onError: (_error, _body, saved) => restore(queryClient, saved),
    onSettled: () => refresh(queryClient),
  });

  const { mutate } = update;
  const { mutate: createMutate } = create;
  // Stable helpers: pages put them into memoised command lists.
  return useMemo(
    () => ({
      create: createMutate,
      toggleDone: (todo: Todo) =>
        mutate({ todo, patch: { status: todo.status === "done" ? "open" : "done" } }),
      setDueDate: (todo: Todo, dueDate: string | null) =>
        mutate({ todo, patch: { due_date: dueDate } }),
      rename: (todo: Todo, title: string) => {
        const trimmed = title.trim();
        if (trimmed && trimmed !== todo.title) mutate({ todo, patch: { title: trimmed } });
      },
      /** Dismisses the todo; the toast offers to bring it back. */
      dismiss: (todo: Todo) =>
        mutate(
          { todo, patch: { status: "dismissed" } },
          {
            onSuccess: (dismissed) =>
              toast(t("tasks.dismissed"), {
                id: `todo-dismissed-${todo.id}`,
                action: {
                  label: t("tasks.undo"),
                  onClick: () => mutate({ todo: dismissed, patch: { status: todo.status } }),
                },
              }),
          },
        ),
    }),
    [createMutate, mutate, t],
  );
}

export function isOptimistic(todo: Todo) {
  return todo.id.startsWith("optimistic-");
}
