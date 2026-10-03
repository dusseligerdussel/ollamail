import type { Page, Route } from "@playwright/test";

/**
 * In-browser mock of the admin API (users, sign-in methods, role mapping, SCIM, shared
 * mailboxes, AI, retention, audit log, organization categories) and of the task export
 * settings, with synthetic data (invented names, `example.org` addresses). Register after
 * `mockApi`; later routes take precedence, unknown paths fall through to `mockApi`.
 */
export interface MockAdmin {
  /** Empty lists and nothing configured (empty states). */
  empty?: boolean;
}

const ORIGIN = "https://mail.example.org";
const CREATED = "2026-09-01T08:00:00Z";

/** The signed-in admin of `mockApi`. */
const adminId = "00000000-0000-4000-8000-000000000001";

export function adminUserId(index: number) {
  return `0192f000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

export const sharedMailboxIds = {
  support: "0192f100-0000-7000-8000-000000000001",
  accounting: "0192f100-0000-7000-8000-000000000002",
};

function adminUser(fields: Record<string, unknown>) {
  return {
    role: "user",
    language: "de",
    timezone: "Europe/Berlin",
    is_active: true,
    created_at: CREATED,
    last_login_at: null,
    providers: [],
    invitation_pending: false,
    active_sessions: 0,
    ...fields,
  };
}

const users = [
  adminUser({
    id: adminId,
    email: "admin@example.org",
    display_name: "Test Admin",
    role: "admin",
    language: "en",
    timezone: "UTC",
    created_at: "2026-01-01T00:00:00Z",
    last_login_at: "2026-10-02T07:45:00Z",
    providers: ["local"],
    active_sessions: 1,
  }),
  adminUser({
    id: adminUserId(1),
    email: "jonas@example.org",
    display_name: "Jonas Beispiel",
    last_login_at: "2026-10-01T16:12:00Z",
    providers: ["oidc:entra"],
    active_sessions: 2,
  }),
  adminUser({
    id: adminUserId(2),
    email: "lena.muster@example.org",
    display_name: "Lena Muster",
    role: "admin",
    last_login_at: "2026-09-30T09:05:00Z",
    providers: ["github:github", "oidc:entra"],
    active_sessions: 1,
  }),
  adminUser({
    id: adminUserId(3),
    email: "mira@example.org",
    display_name: "Mira Testfrau",
    created_at: "2026-09-29T14:00:00Z",
    invitation_pending: true,
  }),
  adminUser({
    id: adminUserId(4),
    email: "paul@example.org",
    display_name: "Paul Platzhalter",
    is_active: false,
    last_login_at: "2026-07-14T11:30:00Z",
    providers: ["ldap:corp"],
  }),
];

const oidcProviders = [
  {
    name: "entra",
    provider: "oidc:entra",
    source: "db",
    display_name: "Microsoft",
    preset: "entra",
    issuer: "https://login.microsoftonline.com/11111111-1111-4111-8111-111111111111/v2.0",
    client_id: "5f0c2a7e-0000-4000-8000-00000000c1d1",
    has_client_secret: true,
    scopes: ["openid", "email", "profile"],
    enabled: true,
    auto_provision: true,
    link_by_email: false,
    allowed_domains: ["example.org"],
    groups_claim: "groups",
    allowed_tenants: ["11111111-1111-4111-8111-111111111111"],
    hosted_domains: [],
    redirect_uri: `${ORIGIN}/api/auth/oidc/entra/callback`,
    created_at: CREATED,
    updated_at: "2026-09-15T10:00:00Z",
  },
];

const githubProviders = [
  {
    name: "github",
    provider: "github:github",
    display_name: "GitHub",
    base_url: null,
    client_id: "Iv1.0000example0000",
    has_client_secret: true,
    enabled: true,
    auto_provision: false,
    link_by_email: true,
    allowed_domains: [],
    allowed_organizations: ["example-org"],
    allowed_teams: ["example-org/it"],
    redirect_uri: `${ORIGIN}/api/auth/github/github/callback`,
    created_at: CREATED,
    updated_at: CREATED,
  },
];

const ldapDirectories = [
  {
    id: "0192f200-0000-7000-8000-00000000d001",
    name: "corp",
    provider: "ldap:corp",
    display_name: "Firmenverzeichnis",
    enabled: true,
    bind_password_set: true,
    created_at: CREATED,
    updated_at: CREATED,
    settings: {
      directory_type: "active_directory",
      server_urls: ["ldaps://dc1.example.org", "ldaps://dc2.example.org"],
      tls_mode: "ldaps",
      ca_certificate: null,
      bind_dn: "CN=svc-ollamail,OU=Service,DC=example,DC=org",
      user_base_dn: "OU=Staff,DC=example,DC=org",
      user_filter: "(sAMAccountName={login})",
      subject_attribute: "objectGUID",
      email_attribute: "mail",
      display_name_attribute: "displayName",
      group_base_dn: "OU=Groups,DC=example,DC=org",
      group_filter: "(objectClass=group)",
      group_member_attribute: "member",
      nested_groups: true,
      allowed_groups: [],
      admin_groups: ["CN=Mail-Admins,OU=Groups,DC=example,DC=org"],
      connect_timeout: 5,
      operation_timeout: 10,
    },
  },
];

const samlProviders = [
  {
    name: "adfs",
    provider: "saml:adfs",
    display_name: "AD FS",
    preset: "adfs",
    metadata_url: "https://adfs.example.org/FederationMetadata/2007-06/FederationMetadata.xml",
    metadata_refreshed_at: "2026-10-01T08:00:00Z",
    idp_entity_id: "http://adfs.example.org/adfs/services/trust",
    idp_sso_url: "https://adfs.example.org/adfs/ls/",
    idp_certificates: [
      { fingerprint_sha256: "ab".repeat(32), not_valid_after: "2027-10-01T00:00:00Z" },
    ],
    sp_entity_id: null,
    effective_sp_entity_id: `${ORIGIN}/api/auth/saml/adfs/metadata`,
    redirect_uri: `${ORIGIN}/api/auth/saml/acs`,
    sp_metadata_url: `${ORIGIN}/api/auth/saml/adfs/metadata`,
    name_id_format: "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    subject_attribute: null,
    email_attribute: "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    display_name_attribute: "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    groups_attribute: "http://schemas.xmlsoap.org/claims/Group",
    trust_email: false,
    enabled: false,
    auto_provision: true,
    link_by_email: false,
    allowed_domains: [],
    created_at: CREATED,
    updated_at: CREATED,
  },
];

function authSettings(empty: boolean) {
  return {
    local_login_enabled: true,
    local_registration: false,
    provider_kinds: ["github", "ldap", "oidc", "saml"],
    admin_access: { usable_admins: empty ? 1 : 2, own_providers: ["local"] },
    mfa_enforcement: empty ? "off" : "admins",
  };
}

function roleMapping(empty: boolean) {
  if (empty) return { enabled: false, default_role: "user", rules: [] };
  const rule = (index: number, provider: string | null, group: string, role: string) => ({
    id: `0192f300-0000-7000-8000-${index.toString(16).padStart(12, "0")}`,
    provider,
    group,
    role,
  });
  return {
    enabled: true,
    default_role: "user",
    rules: [
      rule(1, "oidc:entra", "ollamail-admins", "admin"),
      rule(2, "ldap:corp", "CN=Mail-Admins,OU=Groups,DC=example,DC=org", "admin"),
      rule(3, "github:github", "example-org/it", "admin"),
      rule(4, null, "mail-users", "user"),
    ],
  };
}

function scim(empty: boolean) {
  return {
    enabled: !empty,
    endpoint_url: `${ORIGIN}/api/scim/v2`,
    link_providers: empty ? [] : ["oidc:entra"],
    tokens: empty
      ? []
      : [
          {
            id: "0192f400-0000-7000-8000-000000000001",
            name: "Entra provisioning",
            hint: "x7Qa",
            created_at: CREATED,
            expires_at: "2027-09-01T08:00:00Z",
            last_used_at: "2026-10-02T09:00:00Z",
          },
          {
            id: "0192f400-0000-7000-8000-000000000002",
            name: "Migration script",
            hint: "m2Lk",
            created_at: "2026-09-20T12:00:00Z",
            expires_at: null,
            last_used_at: null,
          },
        ],
    stats: empty
      ? { users: 0, active_users: 0, groups: 0 }
      : { users: 14, active_users: 12, groups: 3 },
  };
}

function syncStatus(fields: Record<string, unknown> = {}) {
  return {
    phase: "idle",
    last_synced_at: "2026-10-02T09:27:00Z",
    last_error: null,
    sync_queued: false,
    folders_total: 4,
    folders_imported: 4,
    folders_failed: 0,
    message_count: 2318,
    ...fields,
  };
}

function assignment(index: number, fields: Record<string, unknown>) {
  return {
    id: `0192f500-0000-7000-8000-${index.toString(16).padStart(12, "0")}`,
    user_id: null,
    user_display_name: null,
    group: null,
    provider: null,
    permission: "read",
    ...fields,
  };
}

const sharedMailboxes = [
  {
    id: sharedMailboxIds.support,
    type: "imap",
    display_name: "Support",
    address: "support@example.org",
    is_shared: true,
    permissions: ["read", "sync", "manage", "act", "send"],
    provider_settings: { host: "imap.example.org", port: 993, security: "tls" },
    has_credentials: true,
    sync_enabled: true,
    sync_settings: { excluded_roles: ["trash", "junk"], poll_interval_seconds: 300 },
    status: syncStatus(),
    created_at: CREATED,
    updated_at: "2026-09-20T08:00:00Z",
    assignments: [
      assignment(1, { user_id: adminUserId(1), user_display_name: "Jonas Beispiel" }),
      assignment(2, { group: "support-team", provider: "oidc:entra", permission: "act" }),
    ],
    reader_count: 6,
  },
  {
    id: sharedMailboxIds.accounting,
    type: "graph",
    display_name: "Buchhaltung",
    address: "buchhaltung@example.org",
    is_shared: true,
    permissions: ["read", "sync", "manage", "act", "send"],
    provider_settings: {},
    has_credentials: true,
    sync_enabled: true,
    sync_settings: { initial_sync_days: 365 },
    status: syncStatus({
      phase: "importing",
      last_synced_at: null,
      folders_total: 6,
      folders_imported: 2,
      message_count: 412,
    }),
    created_at: "2026-09-28T13:00:00Z",
    updated_at: "2026-09-28T13:00:00Z",
    assignments: [assignment(3, { user_id: adminUserId(2), user_display_name: "Lena Muster" })],
    reader_count: 1,
  },
];

function sharedFolders(mailboxId: string) {
  const roles = [
    ["INBOX", "inbox", 1840],
    ["Erledigt", "archive", 452],
    ["Gesendet", "sent", 26],
    ["Papierkorb", "trash", 0],
  ] as const;
  return roles.map(([name, role, count], index) => ({
    id: `${mailboxId.slice(0, 24)}${(0xf0 + index).toString(16).padStart(12, "0")}`,
    remote_id: name,
    name,
    kind: "folder",
    role,
    sync_enabled: role !== "trash",
    excluded_by_role: role === "trash",
    synced: role !== "trash",
    last_synced_at: role === "trash" ? null : "2026-10-02T09:27:00Z",
    last_error: null,
    import_pending: false,
    message_count: count,
  }));
}

function members(mailboxId: string) {
  const mailbox = sharedMailboxes.find((item) => item.id === mailboxId);
  return (mailbox?.assignments ?? [])
    .filter((item) => item.user_id)
    .map((item) => ({ id: item.user_id, display_name: item.user_display_name }));
}

const aiProviders = [
  {
    name: "default",
    display_name: "Ollama (lokal)",
    kind: "ollama",
    base_url: "http://ollama:11434",
    api_key_set: false,
    is_cloud: false,
    structured_output: "native",
    timeout: 300,
    source: "environment",
    used_by: ["triage", "todos", "rag_chat", "reply_draft", "embeddings"],
  },
  {
    name: "cloud",
    display_name: "Example Cloud",
    kind: "openai_compatible",
    base_url: "https://api.cloud.example/v1",
    api_key_set: true,
    is_cloud: true,
    structured_output: "native",
    timeout: 120,
    source: "database",
    used_by: ["digest"],
  },
];

const profiles = [
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
];

const tasks = ["triage", "todos", "digest", "rag_chat", "reply_draft", "embeddings"] as const;

function aiSettings(empty: boolean) {
  return {
    cloud_enabled: !empty,
    cloud_enabled_default: false,
    profile: "cpu",
    profile_default: "cpu",
    profiles,
    context_tokens: 8192,
    concurrency: empty ? 1 : 2,
    concurrency_default: 1,
    concurrency_max: 4,
    tasks: tasks.map((task) => {
      const model = task === "embeddings" ? "bge-m3" : "qwen2.5:3b";
      const cloud = !empty && task === "digest";
      return {
        task,
        provider: cloud ? "cloud" : null,
        model: cloud ? "example-large" : null,
        effective_provider: cloud ? "cloud" : "default",
        effective_model: cloud ? "example-large" : model,
        default_provider: "default",
        default_model: model,
        blocked: false,
      };
    }),
  };
}

function aiModels(name: string) {
  return {
    ok: true,
    models:
      name === "cloud"
        ? ["example-large", "example-small"]
        : ["bge-m3", "qwen2.5:3b", "qwen2.5:7b", "llama3.2:3b"],
    duration_ms: 42,
  };
}

const retentionDefaults = {
  mail_days: 0,
  attachment_days: 0,
  search_index_days: 0,
  rag_history_days: 90,
  digest_days: 30,
  audit_days: 365,
};

function retention(empty: boolean) {
  if (empty) {
    return {
      values: retentionDefaults,
      defaults: retentionDefaults,
      overridden: [],
      initial_sync_days: 90,
      last_run: null,
    };
  }
  return {
    values: { ...retentionDefaults, mail_days: 730, attachment_days: 365, audit_days: 730 },
    defaults: retentionDefaults,
    overridden: ["mail_days", "attachment_days", "audit_days"],
    initial_sync_days: 90,
    last_run: {
      finished_at: "2026-10-02T03:00:12Z",
      mails: 128,
      threads: 41,
      attachments: 37,
      search_chunks: 512,
      audit_events: 0,
    },
  };
}

function categoryId(index: number) {
  return `0192f600-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

const organizationCategories = [
  ...(["important", "action_required", "waiting_for", "info", "newsletter"] as const).map(
    (key, index) => ({
      id: categoryId(index + 1),
      name: key,
      description: "",
      builtin_key: key,
      position: index,
    }),
  ),
  {
    id: categoryId(10),
    name: "Rechnungen",
    description: "Rechnungen, Mahnungen und Zahlungsavise von Lieferanten",
    builtin_key: null,
    position: 5,
  },
  {
    id: categoryId(11),
    name: "Bewerbungen",
    description: "Bewerbungen und Rückfragen von Bewerberinnen und Bewerbern",
    builtin_key: null,
    position: 6,
  },
];

function event(id: number, occurredAt: string, fields: Record<string, unknown>) {
  return {
    id,
    occurred_at: occurredAt,
    actor_kind: "user",
    actor_id: adminId,
    actor_name: "Test Admin",
    target_type: null,
    target_id: null,
    target_name: null,
    details: {},
    ...fields,
  };
}

const auditEvents = [
  event(112, "2026-10-02T09:12:00Z", {
    action: "ai.settings_changed",
    target_type: "settings",
    target_name: "ai",
    details: { field: "concurrency" },
  }),
  event(111, "2026-10-02T08:41:00Z", {
    action: "mailbox.shared",
    target_type: "mailbox",
    target_id: sharedMailboxIds.accounting,
    target_name: "buchhaltung@example.org",
    details: { assignments: 1 },
  }),
  event(110, "2026-10-02T07:45:00Z", {
    action: "auth.login_succeeded",
    target_type: "session",
    details: { provider: "local" },
  }),
  event(109, "2026-10-01T22:03:00Z", {
    action: "auth.login_failed",
    actor_kind: "anonymous",
    actor_id: null,
    actor_name: null,
    details: { provider: "local", reason: "invalid_credentials" },
  }),
  event(108, "2026-10-01T16:12:00Z", {
    action: "auth.login_succeeded",
    actor_id: adminUserId(1),
    actor_name: "Jonas Beispiel",
    target_type: "session",
    details: { provider: "oidc:entra" },
  }),
  event(107, "2026-09-30T10:20:00Z", {
    action: "user.deactivated",
    target_type: "user",
    target_id: adminUserId(4),
    target_name: "Paul Platzhalter",
  }),
  event(106, "2026-09-29T14:00:00Z", {
    action: "user.invited",
    target_type: "user",
    target_id: adminUserId(3),
    target_name: "Mira Testfrau",
  }),
  event(105, "2026-09-28T13:00:00Z", {
    action: "mailbox.created",
    target_type: "mailbox",
    target_id: sharedMailboxIds.accounting,
    target_name: "buchhaltung@example.org",
  }),
  event(104, "2026-09-15T10:00:00Z", {
    action: "idp.config_changed",
    target_type: "idp",
    target_id: "oidc:entra",
    target_name: "Microsoft",
    details: { change: "updated" },
  }),
  event(103, "2026-09-02T03:00:00Z", {
    action: "data.retention_changed",
    actor_kind: "user",
    target_type: "settings",
    target_name: "retention",
    details: { mail_days: 730 },
  }),
  event(102, "2026-09-01T03:00:12Z", {
    action: "crypto.keys_rotated",
    actor_kind: "system",
    actor_id: null,
    actor_name: null,
  }),
];

function todoExport(empty: boolean) {
  return {
    available_sinks: ["caldav", "mstodo", "gtasks"],
    target: empty
      ? null
      : {
          sink: "caldav",
          url: "https://dav.example.org/calendars/admin/",
          username: "admin@example.org",
          has_password: true,
          list_id: "/calendars/admin/aufgaben/",
          list_name: "Aufgaben",
          mode: "auto",
          active: true,
          last_sync_at: "2026-10-02T09:15:00Z",
          last_error: null,
          counts: { synced: 18, pending: 2, error: 1, removed: 3 },
          created_at: CREATED,
        },
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

const llmTasks = ["triage", "todos", "digest", "rag_chat", "reply_draft", "embeddings"] as const;

/** Model state per task; `missing` tasks can be downloaded (Ollama). */
export function systemModels(
  states: Partial<Record<(typeof llmTasks)[number], "installed" | "missing" | "unreachable">> = {},
  pull: Record<string, unknown> | null = null,
) {
  return llmTasks.map((task) => {
    const state = states[task] ?? "installed";
    return {
      task,
      endpoint: "default",
      provider: "ollama",
      model: task === "embeddings" ? "bge-m3" : "qwen2.5:3b",
      state,
      can_pull: state === "missing",
      pull: state === "missing" ? pull : null,
    };
  });
}

export const processingMailboxIds = {
  work: "0192f200-0000-7000-8000-000000000001",
};

/** Checklist facts and processing counts; counts only, never mail content. */
export function systemOverview(empty: boolean) {
  return {
    mailbox_count: empty ? 0 : 2,
    digest_enabled: false,
    digest_scheduler_enabled: true,
    public_url_set: !empty,
    mailboxes: empty
      ? []
      : [
          {
            id: processingMailboxIds.work,
            type: "imap",
            display_name: null,
            is_shared: false,
            owner_name: "Erika Muster",
            sync_phase: "idle",
            sync_error: null,
            processing_enabled: true,
            pending: 12,
            running: 1,
            failed: 3,
          },
          {
            id: sharedMailboxIds.support,
            type: "graph",
            display_name: "Support",
            is_shared: true,
            owner_name: null,
            sync_phase: "error",
            sync_error: "authentication_failed",
            processing_enabled: true,
            pending: 0,
            running: 0,
            failed: 0,
          },
        ],
  };
}

function notFound(route: Route) {
  return route.fulfill({
    status: 404,
    contentType: "application/problem+json",
    json: { type: "about:blank", title: "Not Found", status: 404 },
  });
}

export async function mockAdmin(page: Page, { empty = false }: MockAdmin = {}) {
  const list = <T>(items: T[]) => (empty ? [] : items);
  const routes: Record<string, unknown> = {
    "GET /api/ai/status": {
      cloud: empty ? [] : [{ provider: "cloud", display_name: "Example Cloud", tasks: ["digest"] }],
    },
    "GET /api/admin/ai/settings": aiSettings(empty),
    "GET /api/admin/ai/providers": empty ? aiProviders.slice(0, 1) : aiProviders,
    // A fresh instance: no models downloaded yet; otherwise only the embedding model is missing.
    "GET /api/admin/system/models": empty
      ? systemModels(Object.fromEntries(llmTasks.map((task) => [task, "missing"])))
      : systemModels({ embeddings: "missing" }),
    "GET /api/admin/system/overview": systemOverview(empty),
    "GET /api/audit/events": { items: list(auditEvents), next_before: null },
    "GET /api/triage/organization/categories": list(organizationCategories),
    "GET /api/admin/privacy/retention": retention(empty),
    "GET /api/admin/auth/settings": authSettings(empty),
    "GET /api/admin/auth/role-mapping": roleMapping(empty),
    "GET /api/admin/auth/oidc/providers": list(oidcProviders),
    "GET /api/admin/auth/github/providers": list(githubProviders),
    "GET /api/admin/auth/saml/providers": list(samlProviders),
    "GET /api/auth/ldap/directories": list(ldapDirectories),
    "GET /api/admin/scim": scim(empty),
    "GET /api/admin/shared-mailboxes": list(sharedMailboxes),
    "GET /api/users": empty ? [] : users,
    "GET /api/todo-export": todoExport(empty),
    "GET /api/todo-export/lists": list([
      { id: "/calendars/admin/aufgaben/", name: "Aufgaben" },
      { id: "/calendars/admin/privat/", name: "Privat" },
    ]),
  };

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => {
      const request = route.request();
      const method = request.method();
      const path = new URL(request.url()).pathname;
      const key = `${method} ${path}`;
      if (key in routes) return json(route, routes[key]);

      // Model list of a provider (the settings page asks via a connection test).
      const providerTest = path.match(/^\/api\/admin\/ai\/providers\/([^/]+)\/test$/);
      if (method === "POST" && providerTest) return json(route, aiModels(providerTest[1] ?? ""));

      const shared = path.match(/^\/api\/admin\/shared-mailboxes\/([^/]+)(\/folders)?$/);
      if (method === "GET" && shared) {
        const mailbox = empty ? undefined : sharedMailboxes.find((item) => item.id === shared[1]);
        if (!mailbox) return notFound(route);
        return json(route, shared[2] ? sharedFolders(mailbox.id) : mailbox);
      }
      const memberList = path.match(/^\/api\/mailboxes\/([^/]+)\/members$/);
      if (method === "GET" && memberList && !empty)
        return json(route, members(memberList[1] ?? ""));

      return route.fallback();
    },
  );
}
