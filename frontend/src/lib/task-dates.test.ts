import { describe, expect, it } from "vitest";

import type { Todo } from "@/api/todos";
import { testTodo } from "@/test/todos";

import {
  addDays,
  daysBetween,
  formatDue,
  groupTasks,
  nextWeek,
  taskGroup,
  todayIn,
} from "./task-dates";

describe("task dates", () => {
  it("takes today from the user's time zone, not the browser's", () => {
    const lateEvening = new Date("2026-10-02T23:30:00Z");
    expect(todayIn("UTC", lateEvening)).toBe("2026-10-02");
    expect(todayIn("Europe/Berlin", lateEvening)).toBe("2026-10-03");
    expect(todayIn("America/Los_Angeles", lateEvening)).toBe("2026-10-02");
    // An unknown zone falls back to the browser's instead of failing.
    expect(todayIn("Mars/Olympus", lateEvening)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("calculates with calendar days across months and DST", () => {
    expect(addDays("2026-10-31", 1)).toBe("2026-11-01");
    expect(addDays("2026-03-28", 2)).toBe("2026-03-30");
    expect(addDays("2026-01-01", -1)).toBe("2025-12-31");
    expect(daysBetween("2026-10-02", "2026-10-25")).toBe(23);
    expect(daysBetween("2026-10-02", "2026-09-30")).toBe(-2);
  });

  it("picks next Monday for next week", () => {
    expect(nextWeek("2026-10-02")).toBe("2026-10-05"); // Friday
    expect(nextWeek("2026-10-04")).toBe("2026-10-05"); // Sunday
    expect(nextWeek("2026-10-05")).toBe("2026-10-12"); // Monday
  });

  it("formats due dates relative to today", () => {
    const today = "2026-10-02";
    expect(formatDue("2026-10-02", today, "en")).toBe("Today");
    expect(formatDue("2026-10-03", today, "en")).toBe("Tomorrow");
    expect(formatDue("2026-10-01", today, "en")).toBe("Yesterday");
    expect(formatDue("2026-10-02", today, "de")).toBe("Heute");
    expect(formatDue("2026-10-06", today, "en")).toBe("Tuesday");
    expect(formatDue("2026-10-06", today, "de")).toBe("Dienstag");
    expect(formatDue("2026-10-20", today, "en")).toBe("Oct 20");
    expect(formatDue("2026-09-20", today, "de")).toBe("20. Sept.");
    expect(formatDue("2027-01-15", today, "en")).toBe("Jan 15, 2027");
  });

  it("groups tasks in display order and leaves out dismissed ones", () => {
    const today = "2026-10-02";
    const todos: Todo[] = [
      testTodo(1, { title: "later", due_date: "2026-10-20" }),
      testTodo(2, { title: "soon", due_date: "2026-10-05" }),
      testTodo(3, { title: "late", due_date: "2026-09-30" }),
      testTodo(4, { title: "now", due_date: today }),
      testTodo(5, { title: "whenever", due_date: null }),
      testTodo(6, { title: "gone", status: "dismissed" }),
      testTodo(7, { title: "done first", status: "done", completed_at: "2026-10-01T08:00:00Z" }),
      testTodo(8, { title: "done last", status: "done", completed_at: "2026-10-02T08:00:00Z" }),
    ];

    const groups = groupTasks(todos, today);

    expect(
      groups.map(({ group, todos: items }) => [group, items.map((todo) => todo.title)]),
    ).toEqual([
      ["overdue", ["late"]],
      ["today", ["now"]],
      ["upcoming", ["soon", "later"]],
      ["someday", ["whenever"]],
      ["done", ["done last", "done first"]],
    ]);
    expect(groupTasks([], today)).toEqual([]);
    // Done wins over the date.
    expect(taskGroup(testTodo(9, { status: "done", due_date: "2026-09-01" }), today)).toBe("done");
  });
});
