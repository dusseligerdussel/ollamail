import { queryOptions } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type Todo = components["schemas"]["TodoRead"];
export type TodoCreate = components["schemas"]["TodoCreate"];
export type TodoUpdate = components["schemas"]["TodoUpdate"];
export type TodoStatus = components["schemas"]["TodoStatus"];

/**
 * Query keys. The task page lists live under `["todo"]`. The tasks of a mail live under
 * `["message", …]`, so `message.processed` (new todos after extraction) refreshes them by the
 * default rule in `use-events.ts`.
 */
export const todoKeys = {
  all: ["todo"] as const,
  list: (status: TodoStatus) => ["todo", "list", status] as const,
  message: (messageId: string) => ["message", "todos", messageId] as const,
};

/** Open tasks are all loaded (the page groups them); done ones only the latest. */
export const OPEN_LIMIT = 500;
export const DONE_LIMIT = 50;

export function todosQueryOptions(status: Extract<TodoStatus, "open" | "done">) {
  return queryOptions({
    queryKey: todoKeys.list(status),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/todos", {
          params: {
            query: { status: [status], limit: status === "open" ? OPEN_LIMIT : DONE_LIMIT },
          },
          signal,
        }),
      ),
  });
}

export function messageTodosQueryOptions(messageId: string) {
  return queryOptions({
    queryKey: todoKeys.message(messageId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/todos", {
          params: { query: { message_id: messageId, status: ["open", "done"] } },
          signal,
        }),
      ),
  });
}

export function createTodo(body: TodoCreate) {
  return unwrap(api.POST("/todos", { body }));
}

export function updateTodo(todoId: string, body: TodoUpdate) {
  return unwrap(api.PATCH("/todos/{todo_id}", { params: { path: { todo_id: todoId } }, body }));
}
