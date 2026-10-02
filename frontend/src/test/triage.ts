import type { Category, OrganizationCategory, Triage, TriagedMessage } from "@/api/triage";

import { json } from "./fetch";
import { testMessage } from "./mail";

/** Synthetic triage data for component tests. */
export function categoryId(index: number) {
  return `0199e000-0000-7000-8000-${String(index).padStart(12, "0")}`;
}

const builtins = ["important", "action_required", "waiting_for", "info", "newsletter"] as const;

export function testCategories(): Category[] {
  return [
    ...builtins.map((key, index) => ({
      id: categoryId(index + 1),
      name: key,
      description: `Description of ${key}`,
      builtin_key: key,
      scope: "organization" as const,
      hidden: false,
      position: index,
    })),
    {
      id: categoryId(9),
      name: "Project Apollo",
      description: "Mails about the Apollo project",
      builtin_key: null,
      scope: "user",
      hidden: false,
      position: 9,
    },
  ];
}

export function testOrganizationCategories(): OrganizationCategory[] {
  return testCategories()
    .filter((category) => category.scope === "organization")
    .map(({ id, name, description, builtin_key, position }) => ({
      id,
      name,
      description,
      builtin_key,
      position,
    }));
}

export function testTriage(messageId: string, overrides: Partial<Triage> = {}): Triage {
  return {
    message_id: messageId,
    category_id: categoryId(2),
    priority: 2,
    reason: "The sender asks for a decision.",
    rule: null,
    source: "llm",
    model: "test-model",
    prompt_version: "triage@1",
    updated_at: "2026-10-02T08:00:00Z",
    ...overrides,
  };
}

export function testTriagedMessage(
  index: number,
  categoryIndex: number | null,
  priority: number | null = 2,
): TriagedMessage {
  return {
    ...testMessage(index),
    category_id: categoryIndex === null ? null : categoryId(categoryIndex),
    priority,
  };
}

/**
 * Triage endpoints over in-memory results; records the batch queries and corrections.
 * Returns `undefined` for other requests.
 */
export function triageApi({
  categories = testCategories(),
  results = [] as Triage[],
  inbox = [] as TriagedMessage[],
} = {}) {
  const batches: string[][] = [];
  const corrections: { id: string; body: { category_id: string; priority: number } }[] = [];
  const inboxQueries: URLSearchParams[] = [];
  const byId = new Map(results.map((result) => [result.message_id, result]));

  async function handle(request: Request): Promise<Response | undefined> {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/triage/categories") return json(categories);
    if (route === "GET /api/triage/messages") {
      const ids = url.searchParams.getAll("ids");
      batches.push(ids);
      return json(ids.flatMap((id) => byId.get(id) ?? []));
    }
    const put = /^PUT \/api\/triage\/messages\/([^/]+)$/.exec(route);
    if (put?.[1]) {
      const body = (await request.json()) as { category_id: string; priority: number };
      corrections.push({ id: put[1], body });
      const result = testTriage(put[1], { ...body, source: "user", reason: null });
      byId.set(put[1], result);
      return json(result);
    }
    if (route === "GET /api/triage/inbox/messages") {
      inboxQueries.push(url.searchParams);
      const category = url.searchParams.get("category");
      const items = inbox.filter(
        (message) =>
          !category ||
          (category === "none" ? message.category_id === null : message.category_id === category),
      );
      const counts = new Map<string | null, number>();
      for (const message of inbox) {
        counts.set(message.category_id, (counts.get(message.category_id) ?? 0) + 1);
      }
      return json({
        items,
        total: items.length,
        next_offset: null,
        groups: [
          ...categories.map((item) => ({ category_id: item.id, total: counts.get(item.id) ?? 0 })),
          { category_id: null, total: counts.get(null) ?? 0 },
        ],
      });
    }
    return undefined;
  }

  return { handle, batches, corrections, inboxQueries };
}
