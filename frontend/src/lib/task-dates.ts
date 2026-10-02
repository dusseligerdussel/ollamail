import type { Todo } from "@/api/todos";

/**
 * Due dates are calendar days (`YYYY-MM-DD`) in the user's time zone. These helpers work on
 * such day strings only, so the browser's own time zone never shifts a date.
 */

/** Today as `YYYY-MM-DD` in the given IANA time zone. */
export function todayIn(timeZone: string, now = new Date()): string {
  // en-CA formats as YYYY-MM-DD.
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone }).format(now);
  } catch {
    return new Intl.DateTimeFormat("en-CA").format(now);
  }
}

function toUtc(day: string) {
  const [year = 1970, month = 1, date = 1] = day.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, date));
}

export function addDays(day: string, days: number): string {
  const date = toUtc(day);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/** Whole days from `from` to `to` (negative if `to` is earlier). */
export function daysBetween(from: string, to: string): number {
  return Math.round((toUtc(to).getTime() - toUtc(from).getTime()) / 86_400_000);
}

/** The next Monday after `today` (a week from today if it is a Monday). */
export function nextWeek(today: string): string {
  const weekday = toUtc(today).getUTCDay(); // 0 = Sunday
  return addDays(today, (8 - weekday) % 7 || 7);
}

function capitalize(text: string, locale: string) {
  return text.charAt(0).toLocaleUpperCase(locale) + text.slice(1);
}

/**
 * Short label for a due date: "Yesterday"/"Today"/"Tomorrow", the weekday within the next
 * week, otherwise day and month (with year if not this year).
 */
export function formatDue(day: string, today: string, locale: string): string {
  const diff = daysBetween(today, day);
  if (Math.abs(diff) <= 1) {
    const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(diff, "day");
    return capitalize(relative, locale);
  }
  const date = toUtc(day);
  if (diff > 1 && diff < 7) {
    return new Intl.DateTimeFormat(locale, { weekday: "long", timeZone: "UTC" }).format(date);
  }
  const sameYear = day.slice(0, 4) === today.slice(0, 4);
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    ...(!sameYear && { year: "numeric" }),
    timeZone: "UTC",
  }).format(date);
}

/** Long form for the detail view and accessible labels, e.g. "Friday, 9 October 2026". */
export function formatDueLong(day: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "full", timeZone: "UTC" }).format(toUtc(day));
}

export const taskGroups = ["overdue", "today", "upcoming", "someday", "done"] as const;
export type TaskGroup = (typeof taskGroups)[number];

export function taskGroup(todo: Todo, today: string): TaskGroup {
  if (todo.status === "done") return "done";
  if (!todo.due_date) return "someday";
  if (todo.due_date < today) return "overdue";
  if (todo.due_date === today) return "today";
  return "upcoming";
}

/**
 * Groups open and done tasks in display order. Open tasks are sorted by due date (stable, so
 * the server order breaks ties, also after an optimistic change); done tasks are listed most
 * recently completed first.
 */
export function groupTasks(todos: readonly Todo[], today: string) {
  const groups: Record<TaskGroup, Todo[]> = {
    overdue: [],
    today: [],
    upcoming: [],
    someday: [],
    done: [],
  };
  for (const todo of todos) {
    if (todo.status === "dismissed") continue;
    groups[taskGroup(todo, today)].push(todo);
  }
  for (const group of ["overdue", "upcoming"] as const) {
    groups[group].sort((a, b) => (a.due_date ?? "").localeCompare(b.due_date ?? ""));
  }
  groups.done.sort((a, b) => (b.completed_at ?? "").localeCompare(a.completed_at ?? ""));
  return taskGroups
    .map((group) => ({ group, todos: groups[group] }))
    .filter(({ todos: items }) => items.length > 0);
}
