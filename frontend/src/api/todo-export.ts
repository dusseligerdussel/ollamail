import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";
import { todoKeys } from "./todos";

export type ExportSettings = components["schemas"]["ExportSettingsRead"];
export type ExportTarget = components["schemas"]["ExportTargetRead"];
export type ExportConnection = components["schemas"]["ExportConnection"];
export type ExportTargetSave = components["schemas"]["ExportTargetSave"];
export type ExportMode = components["schemas"]["ExportMode"];
export type ExportSink = components["schemas"]["ExportConnection"]["sink"];
export type TaskList = components["schemas"]["TaskListRead"];
export type TodoExportState = components["schemas"]["TodoExportState"];

/**
 * Under `["todo"]`, so the `todo.exported` event (sync finished) refreshes the settings with
 * the task lists by the default rule in `use-events.ts`.
 */
export const todoExportKey = [...todoKeys.all, "export"] as const;

export const todoExportQueryOptions = queryOptions({
  queryKey: todoExportKey,
  queryFn: ({ signal }) => unwrap(api.GET("/todo-export", { signal })),
});

export function listTaskLists(body: ExportConnection) {
  return unwrap(api.POST("/todo-export/lists", { body }));
}

function useSettingsMutation<T>(
  mutationFn: (variables: T) => Promise<ExportSettings>,
  errorToast = true,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    meta: { errorToast },
    onSuccess: (settings) => {
      queryClient.setQueryData(todoExportKey, settings);
      void queryClient.invalidateQueries({ queryKey: todoKeys.all });
    },
  });
}

/** Errors are shown in the form. */
export function useSaveTodoExport() {
  return useSettingsMutation(
    (body: ExportTargetSave) => unwrap(api.PUT("/todo-export", { body })),
    false,
  );
}

export function useUpdateExportMode() {
  return useSettingsMutation((mode: ExportMode) =>
    unwrap(api.PATCH("/todo-export", { body: { mode } })),
  );
}

export function useDisconnectTodoExport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.DELETE("/todo-export")),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: todoKeys.all }),
  });
}

export function useSyncTodoExport() {
  return useMutation({ mutationFn: () => unwrap(api.POST("/todo-export/sync")) });
}

export function useExportTodo() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (todoId: string) =>
      unwrap(api.POST("/todo-export/todos/{todo_id}", { params: { path: { todo_id: todoId } } })),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: todoKeys.all }),
  });
}
