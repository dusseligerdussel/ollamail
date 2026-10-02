import type { Todo } from "@/api/todos";

export function todoId(index: number) {
  return `0199d000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

/** A synthetic open task without due date. */
export function testTodo(index: number, overrides: Partial<Todo> = {}): Todo {
  return {
    id: todoId(index),
    title: `Task ${index}`,
    description: null,
    due_date: null,
    priority: "normal",
    status: "open",
    is_manual: false,
    is_edited: false,
    confidence: 0.9,
    done_suggested: false,
    mailbox_id: null,
    message_id: null,
    thread_id: null,
    external_refs: {},
    shared: false,
    assignee_id: null,
    created_at: "2026-10-01T08:00:00Z",
    updated_at: "2026-10-01T08:00:00Z",
    completed_at: null,
    ...overrides,
  };
}
