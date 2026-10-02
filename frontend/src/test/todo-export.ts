import type { ExportSettings, ExportTarget, TaskList } from "@/api/todo-export";

import { type FetchHandler, json, problem } from "./fetch";

/** A connected CalDAV target with invented server and account. */
export function testExportTarget(overrides: Partial<ExportTarget> = {}): ExportTarget {
  return {
    sink: "caldav",
    url: "https://cloud.example.org/remote.php/dav",
    username: "erika",
    has_password: true,
    list_id: "/calendars/erika/tasks/",
    list_name: "Tasks",
    mode: "auto",
    active: true,
    last_sync_at: "2026-10-07T09:00:00Z",
    last_error: null,
    counts: { synced: 12, pending: 0, error: 0, removed: 0 },
    created_at: "2026-10-01T08:00:00Z",
    ...overrides,
  };
}

export const testTaskLists: TaskList[] = [
  { id: "/calendars/erika/tasks/", name: "Tasks" },
  { id: "/calendars/erika/work/", name: "Work" },
];

export interface CapturedRequest {
  method: string;
  path: string;
  body: unknown;
}

/**
 * In-memory export API: `GET/PUT/PATCH/DELETE /api/todo-export`, list discovery and the
 * single-task export. `listsError` makes the discovery fail with that error code.
 */
export function todoExportApi({
  target = null as ExportTarget | null,
  sinks = ["caldav"] as ExportSettings["available_sinks"],
  listsError = undefined as string | undefined,
} = {}) {
  const requests: CapturedRequest[] = [];
  let current = target;
  const settings = (): ExportSettings => ({ available_sinks: sinks, target: current });
  const handle = async (request: Request): Promise<Response | undefined> => {
    const { pathname } = new URL(request.url);
    if (!pathname.startsWith("/api/todo-export")) return undefined;
    const text = request.method === "GET" ? "" : await request.clone().text();
    const body = text ? (JSON.parse(text) as Record<string, unknown>) : undefined;
    if (request.method !== "GET") requests.push({ method: request.method, path: pathname, body });
    const route = `${request.method} ${pathname}`;
    switch (route) {
      case "GET /api/todo-export":
        return json(settings());
      case "POST /api/todo-export/lists":
        return listsError ? problem(422, { error_code: listsError }) : json(testTaskLists);
      case "PUT /api/todo-export": {
        const list = testTaskLists.find((item) => item.id === body?.list_id);
        current = testExportTarget({
          url: String(body?.url),
          username: String(body?.username),
          list_id: list?.id ?? "",
          list_name: list?.name ?? "",
          mode: body?.mode as ExportTarget["mode"],
          last_sync_at: null,
          counts: { synced: 0, pending: 0, error: 0, removed: 0 },
        });
        return json(settings());
      }
      case "PATCH /api/todo-export":
        if (current) current = { ...current, mode: body?.mode as ExportTarget["mode"] };
        return json(settings());
      case "DELETE /api/todo-export":
        current = null;
        return new Response(null, { status: 204 });
      case "POST /api/todo-export/sync":
        return new Response(null, { status: 202 });
      default:
        return problem(404);
    }
  };
  return { requests, handle } satisfies { requests: CapturedRequest[]; handle: unknown };
}

export function withExportApi(api: ReturnType<typeof todoExportApi>, fallback: FetchHandler) {
  return async (request: Request) => (await api.handle(request)) ?? fallback(request);
}
