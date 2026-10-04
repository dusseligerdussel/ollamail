import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { AIConnectionTest, AIProvider, AISettings, AIStatus } from "@/api/ai";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testUser } from "@/test/fetch";
import { setViewportWidth } from "@/test/media";
import { renderApp } from "@/test/render-app";

const providers: AIProvider[] = [
  {
    name: "default",
    display_name: "default",
    kind: "ollama",
    base_url: "http://ollama:11434",
    api_key_set: false,
    is_cloud: false,
    structured_output: "native",
    timeout: 300,
    source: "environment",
    used_by: ["triage", "todos", "rag_chat", "embeddings"],
  },
  {
    name: "cloud",
    display_name: "Example Cloud",
    kind: "openai_compatible",
    base_url: "https://api.example.test/v1",
    api_key_set: true,
    is_cloud: true,
    structured_output: "native",
    timeout: null,
    source: "database",
    used_by: ["digest"],
  },
];

function task(name: AISettings["tasks"][number]["task"], overrides = {}) {
  const model = name === "embeddings" ? "bge-m3" : "qwen2.5:3b";
  return {
    task: name,
    provider: null,
    model: null,
    effective_provider: "default",
    effective_model: model,
    default_provider: "default",
    default_model: model,
    blocked: false,
    ...overrides,
  };
}

const settings: AISettings = {
  cloud_enabled: false,
  cloud_enabled_default: false,
  profile: "cpu",
  profile_default: "cpu",
  profiles: [
    { name: "cpu", chat_model: "qwen2.5:3b", embedding_model: "bge-m3", context_tokens: 8192 },
    {
      name: "gpu-consumer",
      chat_model: "qwen2.5:14b",
      embedding_model: "bge-m3",
      context_tokens: 16384,
    },
    {
      name: "gpu-server",
      chat_model: "qwen2.5:32b",
      embedding_model: "bge-m3",
      context_tokens: 32768,
    },
  ],
  context_tokens: 8192,
  concurrency: 1,
  concurrency_default: 1,
  concurrency_max: 4,
  tasks: [
    task("triage"),
    task("todos"),
    task("digest", {
      provider: "cloud",
      model: "gpt-test",
      effective_provider: "cloud",
      effective_model: "gpt-test",
      blocked: true,
    }),
    task("rag_chat"),
    task("embeddings"),
  ],
};

const ok: AIConnectionTest = { ok: true, models: ["bge-m3", "qwen2.5:3b"], duration_ms: 42 };
const denied: AIConnectionTest = {
  ok: false,
  models: [],
  error: "unauthorized",
  status_code: 401,
  duration_ms: 80,
};

interface Captured {
  method: string;
  path: string;
  body: unknown;
}

function mockAiApi({
  status = { cloud: [] } as AIStatus,
  user = undefined as typeof testUser | undefined,
  providerList = providers,
  // `POST …/providers/test` answers 403 reauth-required until `POST /api/auth/reauth`.
  testNeedsReauth = false,
} = {}) {
  const requests: Captured[] = [];
  const api = backend(user ? { user } : {});
  let confirmed = false;
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    const body = request.method === "GET" ? undefined : await request.clone().text();
    requests.push({
      method: request.method,
      path: pathname,
      body: body ? JSON.parse(body) : undefined,
    });
    switch (route) {
      case "GET /api/ai/status":
        return json(status);
      case "GET /api/admin/ai/settings":
        return json(settings);
      case "GET /api/admin/ai/providers":
        return json(providerList);
      case "POST /api/admin/ai/providers/default/test":
        return json(ok);
      case "POST /api/admin/ai/providers/cloud/test":
        return json(denied);
      case "POST /api/admin/ai/providers/test":
        return testNeedsReauth && !confirmed
          ? problem(403, { type: "urn:ollamail:problem:reauth-required", reauth_minutes: 10 })
          : json(ok);
      case "GET /api/auth/reauth":
        return json({ methods: ["password", "signin"], reauth_minutes: 10, valid_until: null });
      case "POST /api/auth/reauth":
        confirmed = true;
        return json({ authenticated_at: "2026-10-04T10:00:00Z", valid_until: null });
      case "POST /api/admin/ai/providers":
        return json(
          { ...providers[1], name: "lab", display_name: "Lab", source: "database" },
          { status: 201 },
        );
      case "PATCH /api/admin/ai/settings":
        return json({ ...settings, cloud_enabled: true });
      case "DELETE /api/admin/ai/providers/cloud":
        return problem(409);
      default:
        return api(request);
    }
  });
  return requests;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

describe("AI settings", () => {
  it("is not shown to non-admins", async () => {
    mockAiApi({ user: testUser });
    await renderApp("/admin/ai");
    expect(screen.getByRole("heading", { level: 1, name: "No access" })).toBeInTheDocument();
  });

  it("is linked from the admin page", async () => {
    mockAiApi();
    await renderApp("/admin");
    expect(screen.getByRole("link", { name: /Language models/ })).toHaveAttribute(
      "href",
      "/admin/ai",
    );
  });

  it("lists providers and tests connections", async () => {
    const user = userEvent.setup();
    mockAiApi();
    await renderApp("/admin/ai");

    const list = await screen.findByRole("list", { name: "Providers" });
    const [local, cloud] = within(list).getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(local).toHaveTextContent("Environment");
    expect(local).toHaveTextContent("Local");
    expect(within(local).queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
    expect(cloud).toHaveTextContent("Cloud");
    expect(cloud).toHaveTextContent("API key set");
    expect(cloud).toHaveTextContent("Used for: Digest");

    await user.click(within(local).getByRole("button", { name: "Test connection" }));
    expect(await within(local).findByRole("status")).toHaveTextContent(
      "Connected · 2 models · 42 ms",
    );
    await user.click(within(cloud).getByRole("button", { name: "Test connection" }));
    expect(await within(cloud).findByRole("status")).toHaveTextContent(
      "Access denied (HTTP 401). Check the API key.",
    );
  });

  it("adds a provider with a write-only API key", async () => {
    const user = userEvent.setup();
    const requests = mockAiApi();
    await renderApp("/admin/ai");

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const form = await screen.findByRole("form", { name: "Add provider" });
    await user.type(within(form).getByLabelText("Display name"), "Lab");
    await user.type(within(form).getByLabelText("Name"), "lab");
    await user.selectOptions(within(form).getByLabelText("Type"), "OpenAI-compatible");
    await user.type(within(form).getByLabelText("URL"), "http://vllm:8000/v1");
    await user.type(within(form).getByLabelText("API key"), "sk-test");
    await user.click(within(form).getByRole("button", { name: "Test connection" }));
    expect(await within(form).findByRole("status")).toHaveTextContent("Connected");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        requests.find((r) => r.method === "POST" && r.path === "/api/admin/ai/providers")?.body,
      ).toEqual({
        name: "lab",
        display_name: "Lab",
        kind: "openai_compatible",
        base_url: "http://vllm:8000/v1",
        api_key: "sk-test",
        is_cloud: false,
        structured_output: "native",
        timeout: null,
      }),
    );
    expect(requests.find((r) => r.path === "/api/admin/ai/providers/test")?.body).toEqual({
      kind: "openai_compatible",
      base_url: "http://vllm:8000/v1",
      api_key: "sk-test",
    });
  });

  it("asks for the API key or a confirmation when the stored key would go elsewhere", async () => {
    const user = userEvent.setup();
    const requests = mockAiApi({ testNeedsReauth: true });
    await renderApp("/admin/ai");

    const list = await screen.findByRole("list", { name: "Providers" });
    const cloud = within(list).getAllByRole("listitem")[1] as HTMLElement;
    await user.click(within(cloud).getByRole("button", { name: /Edit/ }));
    const form = await screen.findByRole("form", { name: "Edit provider" });
    expect(form).toHaveTextContent("Set. Leave empty to keep it.");

    const url = within(form).getByLabelText("URL");
    await user.clear(url);
    await user.type(url, "https://relay.example.test/v1");
    expect(form).toHaveTextContent("URL or type changed: enter the API key again.");

    await user.click(within(form).getByRole("button", { name: "Test connection" }));
    const sheet = await screen.findByRole("dialog", { name: "Confirm it is you" });
    await user.type(within(sheet).getByLabelText("Password"), "correct horse battery");
    await user.click(within(sheet).getByRole("button", { name: "Confirm" }));

    expect(await within(form).findByRole("status")).toHaveTextContent("Connected");
    const tests = requests.filter((r) => r.path === "/api/admin/ai/providers/test");
    expect(tests).toHaveLength(2);
    expect(tests[1]?.body).toEqual({
      kind: "openai_compatible",
      base_url: "https://relay.example.test/v1",
      name: "cloud",
    });

    // A new key needs no confirmation; the hint goes away.
    await user.type(within(form).getByLabelText("API key"), "sk-new");
    expect(form).not.toHaveTextContent("URL or type changed");
  });

  it("validates the provider form", async () => {
    const user = userEvent.setup();
    mockAiApi();
    await renderApp("/admin/ai");

    await user.click(await screen.findByRole("button", { name: "Add provider" }));
    const form = await screen.findByRole("form", { name: "Add provider" });
    await user.type(within(form).getByLabelText("Name"), "Bad Name");
    await user.type(within(form).getByLabelText("URL"), "ftp://host");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    expect(within(form).getByText(/Only lower-case letters/)).toBeInTheDocument();
    expect(within(form).getByText("Enter an http(s) URL without credentials.")).toBeInTheDocument();
    expect(within(form).getByText("Enter a display name.")).toBeInTheDocument();
  });

  it("assigns a model per task", async () => {
    // Stacked layout: in jsdom the resizable navigation handle catches pointer events.
    setViewportWidth(390);
    const user = userEvent.setup();
    const requests = mockAiApi();
    await renderApp("/admin/ai");

    expect(
      await screen.findByText(
        "Cloud provider, but cloud LLMs are blocked: this task does not run.",
      ),
    ).toBeInTheDocument();
    const triageModel = screen.getAllByLabelText("Model")[0] as HTMLInputElement;
    expect(triageModel).toHaveAttribute("placeholder", "Default: qwen2.5:3b");
    await user.type(triageModel, "llama3.2:3b");
    await user.click(screen.getByRole("button", { name: "Save assignment" }));

    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({
        tasks: { triage: { provider: null, model: "llama3.2:3b" } },
      }),
    );
  });

  it("names the default provider by its display name", async () => {
    mockAiApi({
      providerList: [{ ...(providers[0] as AIProvider), display_name: "Local Ollama" }],
    });
    await renderApp("/admin/ai");

    const triageProvider = (await screen.findAllByLabelText("Provider"))[0] as HTMLSelectElement;
    expect(
      within(triageProvider).getByRole("option", { name: "Default (Local Ollama)" }),
    ).toHaveValue("");
  });

  it("requires a model when a provider is chosen", async () => {
    const user = userEvent.setup();
    mockAiApi();
    await renderApp("/admin/ai");

    const todosProvider = (await screen.findAllByLabelText("Provider"))[1] as HTMLSelectElement;
    await user.selectOptions(todosProvider, "Example Cloud");

    expect(screen.getByText("Enter a model for the chosen provider.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save assignment" })).toBeDisabled();
  });

  it("asks for confirmation before allowing cloud LLMs", async () => {
    const user = userEvent.setup();
    const requests = mockAiApi();
    await renderApp("/admin/ai");

    expect(await screen.findByText("Data is sent to third parties")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Allow cloud LLMs" }));
    const dialog = await screen.findByRole("dialog", { name: "Allow cloud LLMs?" });
    expect(requests.some((r) => r.method === "PATCH")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "Allow" }));

    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({ cloud_enabled: true }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("explains why a provider in use cannot be deleted", async () => {
    const user = userEvent.setup();
    mockAiApi();
    await renderApp("/admin/ai");

    await user.click(await screen.findByRole("button", { name: "Delete: Example Cloud" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The provider is still assigned to tasks.",
    );
  });
});

describe("cloud notice", () => {
  it("tells every user which cloud provider receives mail content", async () => {
    mockAiApi({
      user: testUser,
      status: {
        cloud: [{ provider: "cloud", display_name: "Example Cloud", tasks: ["triage", "digest"] }],
      },
    });
    await renderApp("/inbox");

    const notice = await screen.findByRole("complementary", { name: "Cloud AI active" });
    expect(notice).toHaveTextContent("Example Cloud receives mail content for: Triage, Digest");
  });

  it("is absent while everything runs locally", async () => {
    mockAiApi({ user: testUser });
    await renderApp("/inbox");

    await waitFor(() =>
      expect(
        screen.queryByRole("complementary", { name: "Cloud AI active" }),
      ).not.toBeInTheDocument(),
    );
  });
});
