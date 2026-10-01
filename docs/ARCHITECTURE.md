# Architektur

ollamail ist ein selbst gehostetes, „local first“ E-Mail-Analyse-Tool für Einzelpersonen und
Unternehmen. Es synchronisiert Postfächer, klassifiziert eingehende Mails (Triage), extrahiert
Todos, erzeugt eine tägliche Audio-Zusammenfassung und erlaubt Fragen an die eigene Inbox (RAG).

## 1. Komponenten

```
                ┌─────────────────────────────┐
  Browser ────▶ │ frontend (React, statisch,   │
                │ ausgeliefert via Caddy/nginx)│
                └──────────────┬──────────────┘
                               │ /api (REST, JSON, SSE)
                ┌──────────────▼──────────────┐
                │ api (FastAPI)                │
                │ Auth, REST, SSE, Admin       │
                └──────┬───────────────┬──────┘
                       │               │ enqueue
                ┌──────▼──────┐  ┌─────▼────────────────────┐
                │ PostgreSQL  │◀─│ worker (Procrastinate)    │
                │ + pgvector  │  │ Mail-Sync, Triage, Todos, │
                │ + FTS       │  │ Embeddings, Digest, TTS   │
                │ + Job-Queue │  └─────┬───────────┬─────────┘
                └─────────────┘        │           │
                               ┌───────▼───┐ ┌─────▼──────┐
                               │ LLM-      │ │ Mail-      │
                               │ Provider  │ │ Provider   │
                               │ (Ollama,  │ │ (IMAP,     │
                               │  OpenAI-  │ │  Graph,    │
                               │  kompat.) │ │  Gmail)    │
                               └───────────┘ └────────────┘
  Dateien (Anhänge, Audio): lokales Volume (später optional S3/MinIO)
```

| Container | Aufgabe |
|---|---|
| `frontend` | Statische React-App + Reverse Proxy zu `api` |
| `api` | FastAPI, stateless, horizontal skalierbar |
| `worker` | Hintergrundjobs; mehrere Instanzen möglich; Queues getrennt nach Jobtyp (`sync`, `llm`, `tts`) |
| `scheduler` | Periodische Jobs (Procrastinate periodic tasks; kann im Worker laufen) |
| `postgres` | PostgreSQL 16 mit pgvector |
| `ollama` | Optional über die Compose-Profile `ollama-cpu` bzw. `ollama-gpu` (NVIDIA) |

**Bewusst kein Redis:** Job-Queue (Procrastinate), Sessions, Rate-Limits und Pub/Sub
(`LISTEN/NOTIFY`) laufen über Postgres. Ein Container weniger, transaktionales Enqueue,
einfacheres Backup.

## 2. Backend-Struktur

```
backend/app/
  core/          config, security (Krypto, Sessions), logging, db, deps
  auth/          lokale Accounts, OIDC, LDAP, Rollen, Bootstrap des Erst-Admins
  users/         Nutzer, Gruppen, Rollen-Mapping
  mail/
    providers/   base.py (Interface), registry.py, fake.py (Tests), imap.py, graph.py (später), gmail.py (später)
    sync/        Initialimport, IDLE/Delta-Sync, Ordner-Mapping
    models.py    Mailbox, Folder, Thread, Message, Attachment, SyncState
    mime.py      MIME-Parsing, Zeichensätze, Normalisierung
    sanitize.py  HTML-Sanitizing (nh3), HTML → Text
    quotes.py    Zitate und Signaturen abtrennen
    threads.py   Threading
    service.py   Speichern/Löschen (DB + Dateien)
  ai/
    llm/         Provider-Interface, ollama.py, openai_compat.py, Modellprofile
    embeddings/  Chunking, Embedding-Jobs
    prompts/     versionierte Prompt-Templates
  triage/        Kategorien, Klassifikation, Feedback/Few-Shot
  todos/         Extraktion, CRUD, (später) CalDAV-Export
  digest/        Tageszusammenfassung, TTS, Podcast-Feed
  rag/           Hybrid-Retrieval, Chat, Zitate
  admin/         Instanz-Einstellungen, Auth-Provider, Audit-Log, Statistiken
  audit/         Audit-Events
  worker.py      Procrastinate-App und Task-Registrierung
```

## 3. Zentrale Abstraktionen

### 3.1 Mail-Provider

```python
class MailProvider(Protocol):  # app/mail/providers/base.py
    capabilities: ProviderCapabilities  # labels, push, server_threads, keywords
    async def list_folders(self) -> list[RemoteFolder]: ...
    def fetch_since(self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
                    ) -> AsyncIterator[SyncEvent]: ...  # MessageFetched | MessageUpdated | MessageDeleted | CursorAdvanced
    def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]: ...  # IMAP IDLE / Graph Webhooks / Gmail Push
    async def move(self, remote_ref: str, target_folder_id: str) -> str: ...  # neue Referenz (IMAP-UIDs ändern sich)
    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None: ...
    async def apply_label(self, remote_ref: str, label: str) -> None: ...  # Gmail: Label, Graph: Kategorie, IMAP: Keyword oder Ordner
    async def remove_label(self, remote_ref: str, label: str) -> None: ...
    async def aclose(self) -> None: ...
```

- **Ordner vs. Labels:** `RemoteFolder.kind` (`folder`/`label`); `RawMessage.folder_ids` listet alle
  Ordner/Labels einer Mail. In der DB verbindet `mail_message_folders` Mails und Ordner (n:m).
- **Cursor:** `SyncCursor.data` ist provider-spezifisch und JSON-serialisierbar (IMAP: UIDVALIDITY,
  letzte UID, HIGHESTMODSEQ; Graph: `deltaLink`; Gmail: `historyId`) und liegt in `mail_sync_states`
  – je Ordner oder (Gmail) postfachweit mit `folder_id IS NULL`. `fetch_since` endet immer mit
  `CursorAdvanced`; der Cursor wird erst gespeichert, wenn die vorherigen Änderungen gespeichert sind.
  Ungültige Cursor (`CursorInvalidError`) erzwingen einen Neuabgleich des Ordners.
- **Inhalt:** Alle Provider liefern die RFC-5322-Quelle (IMAP `BODY[]`, Graph `/$value`, Gmail
  `format=raw`), daher ist die Normalisierung (`app/mail/mime.py`) für alle gleich.
- **Registry:** Provider registrieren sich mit `registry.register(MailboxType.X, Factory)`; Features
  nutzen nur `registry.create(config)`. Für Tests anderer Module gibt es `FakeMailProvider`
  (In-Memory-Server mit Änderungslog, IMAP- oder Gmail-Verhalten).

**Normalisierung** (`normalize_message`): MIME-Parsing mit robustem Zeichensatz-Fallback (deklariert →
UTF-8 → Windows-1252, nie Abbruch), HTML → Text (`<blockquote>` wird zu `> `), Abtrennen von Zitaten
(„Am … schrieb“, „On … wrote“, Outlook-Kopfblöcke, `>`) und Signaturen (`-- `, mobile Signaturen),
Spracherkennung offline (`py3langid`). **Threading** pro Postfach: Server-Thread-ID (Gmail/Graph) →
`In-Reply-To`/`References` → Betreff-Fallback (nur für Antworten, 30-Tage-Fenster).

**Anzeige von HTML:** Original-HTML wird gespeichert und erst bei der Anzeige serverseitig mit `nh3`
bereinigt (Allow-List; keine Skripte, Formulare, Frames, Event-Handler, gefährlichen URL-Schemata,
CSS nur ohne Ressourcen/Positionierung). Externe Bilder sind standardmäßig blockiert (Tracking-Schutz),
`cid:`-Bilder werden auf Anhang-URLs umgeschrieben.

**Besitz:** Ein Postfach gehört genau einem Nutzer (`owner_user_id`) oder ist shared
(`is_shared`, per CHECK erzwungen). Der Fremdschlüssel auf `users` und die Zuweisungstabelle für
Shared Mailboxes folgen mit dem Nutzermodell (#11).

| Provider | Phase | Auth | Sync | Hinweise |
|---|---|---|---|---|
| IMAP/SMTP | MVP | Passwort/App-Passwort, später XOAUTH2 | UIDVALIDITY/UID + `IDLE`, optional CONDSTORE | Funktioniert mit jedem Server |
| Microsoft 365 | v1 | OAuth2 (Entra ID App, delegiert oder App-only mit Admin-Consent) | Graph Delta Query + Change Notifications | Shared Mailboxes über App-Permissions + `ApplicationAccessPolicy` |
| Gmail / Google Workspace | v1 | OAuth2, Workspace: Domain-wide Delegation | `history.list` + Pub/Sub Push (optional Polling) | Labels statt Ordner |

Postfach-Zugangsdaten und OAuth-Tokens werden verschlüsselt gespeichert (siehe `PRIVACY.md`;
Spalte `mail_mailboxes.credentials` vom Typ `EncryptedJSON` aus `app/core/crypto.py`).

### 3.2 LLM-Provider

```python
class LLMProvider(Protocol):
    async def complete(self, messages: list[ChatMessage], *, schema: type[BaseModel] | None = None, **opts) -> LLMResult: ...
    async def stream(self, messages: list[ChatMessage], **opts) -> AsyncIterator[str]: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

- Implementierungen: `ollama`, `openai_compatible` (vLLM, LM Studio, LocalAI, llama.cpp-Server,
  auch OpenAI/Azure/Anthropic-Gateways). Cloud-Endpunkte müssen vom Admin explizit freigegeben werden.
- **Strukturierte Ausgaben** (Triage, Todos) immer über JSON-Schema/Pydantic mit Validierung und Retry.
- **Modellprofile** statt hartkodierter Modelle (Beispiele, nicht bindend):

| Profil | Zielhardware | Chat/Klassifikation | Embeddings |
|---|---|---|---|
| `cpu` | Nur CPU, 16 GB RAM | ~1–4B-Modell, quantisiert | `bge-m3` o. ä. (mehrsprachig) |
| `gpu-consumer` | 8–24 GB VRAM | ~7–14B-Modell | `bge-m3` |
| `gpu-server` | Server-GPUs, vLLM | ≥ 30B-Modell, hoher Durchsatz | `bge-m3` |

  Jeder Task (Triage, Todos, Digest, RAG-Chat, Embeddings) kann einem eigenen Modell zugewiesen werden.
- Throttling: Die `llm`-Queue hat eine konfigurierbare Parallelität, damit CPU-Instanzen nicht überlaufen.
- Jeder LLM-Aufruf speichert Modell, Prompt-Version und Dauer (ohne Inhalte) für die Nachvollziehbarkeit.

#### Umsetzung (`backend/app/ai/`)

```
ai/llm/
  base.py           LLMProvider-Protocol (complete, stream, embed, list_models)
  ollama.py         native Ollama-API (/api/chat, /api/embed, /api/tags, /api/pull)
  openai_compat.py  Chat-Completions-API (base_url inkl. /v1)
  config.py         LLMConfigResolver (Protocol) + EnvConfigResolver
  profiles.py       Hardware-Profile cpu / gpu-consumer / gpu-server
  structured.py     Pydantic → JSON-Schema, Validierung, Retry, Prompt-Fallback
  context.py        Token-Schätzung, Kürzen langer Mails
  metrics.py        LLMCallMetrics + MetricsSink (Standard: Log-Event `llm_call`)
  gateway.py        LLMGateway – einziger Einstiegspunkt für Features
ai/prompts/         versionierte, sprachabhängige Prompt-Templates (`name@version`)
```

Features nutzen ausschließlich das `LLMGateway` (`app.state.llm`, FastAPI-Dependency `get_llm`):

```python
triage = await llm.complete_structured(
    LLMTask.TRIAGE, prompt.render(mail_language, mail=text), TriageResult,
    prompt_version=prompt.id, language=mail_language,
)
```

Das Gateway erledigt pro Aufruf:

1. **Auflösung** Task → Endpunkt + Modell über einen `LLMConfigResolver`. In diesem Stand liest
   `EnvConfigResolver` Umgebungsvariablen und Code-Defaults. Die Admin-Einstellungen in der DB (#18)
   ersetzen nur den Resolver; Gateway und Features bleiben unverändert.
   Reihenfolge: `OLLAMAIL_LLM_TASK_<TASK>_MODEL` → `OLLAMAIL_LLM_DEFAULT_{CHAT,EMBEDDING}_MODEL` → Profil.
2. **Cloud-Sperre:** Endpunkte mit `is_cloud` werden nur genutzt, wenn `OLLAMAIL_LLM_CLOUD_ENABLED`
   aktiv ist. Sonst `CloudLLMDisabledError`, bevor ein HTTP-Client entsteht.
3. **Kontextlänge:** Die Prompts werden auf das Kontextfenster des Profils gekürzt
   (konservative Schätzung ≈ 3 Zeichen/Token, Platz für die Antwort wird reserviert). Gekürzt wird die
   längste Nicht-System-Nachricht vom Ende her, weil neue Inhalte in Mails oben stehen.
   Bei Ollama wird `num_ctx` gesetzt, weil Ollama sonst stillschweigend auf ein kleines Fenster kürzt.
4. **Structured Output:** Das JSON-Schema geht nativ an den Server (Ollama `format`, OpenAI
   `response_format`) und zusätzlich in den System-Prompt. Ungültige Antworten werden mit einem
   Korrekturhinweis erneut angefragt (`OLLAMAIL_LLM_STRUCTURED_OUTPUT_RETRIES`, Standard 2). Danach
   folgt `LLMOutputError`. Lehnt ein Server den Schema-Parameter ab (HTTP 400/422), fällt das
   Gateway für dieses Modell dauerhaft auf reines Prompting zurück. Das lässt sich pro Endpunkt
   auch fest einstellen (`structured_output=prompt`).
5. **Metriken:** Task, Endpunkt, Modell, Prompt-Version, Dauer, Token-Zahlen, Versuche und
   Fehlertyp. Prompts und Antworten werden **nie** erfasst. Fehlermeldungen enthalten keine
   Response-Bodies, weil manche Server die Anfrage darin zurückspiegeln.

Readiness: Mit `OLLAMAIL_LLM_READINESS_CHECK=true` prüft `/readyz` (Check `llm`), ob alle zugewiesenen
Modelle auf ihren Endpunkten verfügbar sind. Der Check ist standardmäßig aus, weil die API auch ohne
LLM nutzbar bleibt (Postfächer, Todos, Einstellungen). Mit `OLLAMAIL_LLM_PULL_MISSING_MODELS=true`
lädt die API fehlende Modelle beim Start im Hintergrund aus Ollama.

**Profil-Defaults** (`profiles.py`). Die Modellnamen sind **Beispiele** und lassen sich per Env
überschreiben:

| Profil | Chat-Modell (Beispiel) | Embeddings (Beispiel) | Kontext |
|---|---|---|---|
| `cpu` (Standard) | `qwen2.5:3b` | `bge-m3` | 8192 |
| `gpu-consumer` | `qwen2.5:14b` | `bge-m3` | 16384 |
| `gpu-server` | `qwen2.5:32b` | `bge-m3` | 32768 |

### 3.3 TTS

- Interface `TTSEngine.synthesize(text, voice, lang) -> AudioFile`.
- Standard **Piper**: sehr schnell auf CPU (auch ARM), gute deutsche und englische Stimmen, kleine Modelle.
  Weitere Engines (z. B. Kokoro, XTTS) sind als Plugins möglich, falls GPU vorhanden.
- Ausgabe: Opus/MP3, gespeichert im Daten-Volume, Aufbewahrung konfigurierbar.

### 3.4 Hintergrundjobs & Echtzeit-Events

Umgesetzt in `backend/app/worker.py` und `backend/app/core/events.py`.

- **Procrastinate** mit Postgres als Queue. Das Schema ist eine Alembic-Migration (vendored SQL in
  `backend/migrations/sql/`); Autogenerate ignoriert die `procrastinate_*`-Tabellen.
  Ein Procrastinate-Update mit Schemaänderung braucht eine neue Migration – ein Test schlägt sonst an.
- **Queues** `sync`, `llm`, `tts`, `default`. `OLLAMAIL_WORKER_QUEUES` wählt die Queues eines
  Worker-Prozesses; `llm` hat eine eigene Parallelität (`OLLAMAIL_LLM_CONCURRENCY`), alle anderen
  teilen sich `OLLAMAIL_WORKER_CONCURRENCY`.
- **Task-Konventionen:** idempotent; Argumente nur IDs; Retry mit exponentiellem Backoff
  (`DEFAULT_RETRY`); Lock-Keys pro Ressource (`resource_lock("mailbox", id)` als `lock`/`queueing_lock`);
  Periodic Tasks per `@app.periodic(cron=...)`. Task-Module werden in `TASK_MODULES` eingetragen.
- **Housekeeping:** täglicher Job `worker.remove_old_jobs` löscht abgeschlossene Jobs nach 7 Tagen.
- **Shutdown:** Bei SIGTERM nimmt der Worker keine neuen Jobs an; laufende Jobs haben
  `OLLAMAIL_WORKER_SHUTDOWN_TIMEOUT` Sekunden, dann endet der Prozess mit Exit-Code 0.
- **Events:** `publish(session, user_id, Event(...))` sendet per `pg_notify` beim Commit. Jeder
  API-Prozess hält eine `LISTEN`-Verbindung und verteilt an `GET /api/events` (SSE), gefiltert auf den
  angemeldeten Nutzer. Ein `Event` besteht nur aus `type`, `ids` und `status` (per Pattern validiert).
  Zustellung ist best effort: Nach einem Reconnect lädt der Client seine Daten neu.
- **Aktueller Nutzer:** Dependency `app.core.current_user.get_current_user_id`. Bis zur Auth (#11)
  liefert sie immer 401; Tests überschreiben sie.

## 4. Feature-Pipelines

### 4.1 Eingang einer Mail

```
sync job ─▶ Message gespeichert ─▶ enqueue(process_message)
  process_message:
    1. Normalisieren (HTML→Text, Zitate/Signaturen abtrennen, Sprache erkennen)
    2. triage.classify      → Kategorie, Priorität, Begründung (kurz)
    3. todos.extract        → 0..n Todos (Titel, Fälligkeit, Quelle)
    4. embeddings.index     → Chunks + Vektoren + tsvector
    5. optional: Aktion am Server (Label/Ordner), wenn vom Nutzer aktiviert
  Fortschritt/Ergebnis → NOTIFY → SSE an das Frontend
```

Jeder Schritt ist ein idempotenter Job. Fehler werden protokolliert und erneut versucht (Backoff).

### 4.2 Triage

- Standardkategorien: **Wichtig**, **Handlungsbedarf**, **Warten auf**, **Info**, **Newsletter**, **Benachrichtigung**, **Spam/Werbung**.
- Nutzer (und Admin als Org-Default) können Kategorien mit Beschreibung in natürlicher Sprache anlegen.
- Korrekturen durch den Nutzer werden gespeichert und als Few-Shot-Beispiele genutzt (pro Nutzer, nie nutzerübergreifend).
- Vorfilter ohne LLM (Header wie `List-Unsubscribe`, `Precedence: bulk`, bekannte Absender) sparen Rechenzeit.

### 4.3 Todos

- Extraktion: Titel, Beschreibung, Fälligkeit (falls genannt), Priorität, Link zur Quell-Mail/zum Thread.
- Status: offen / erledigt / verworfen. Duplikaterkennung innerhalb eines Threads.
- **Ziel (dokumentiert, später):** Export/Sync via CalDAV (VTODO), Microsoft To Do (Graph), Google Tasks.

### 4.4 Daily Digest (Audio)

- Periodischer Job pro Nutzer zur eingestellten Uhrzeit und Zeitzone.
- Zusammenfassung der Mails seit dem letzten Digest: wichtige Mails, offene Todos, Termine/Fristen.
- Text → Piper → Audiodatei. Text-Version wird mitgespeichert (Transkript, barrierefrei).
- Auslieferung: Web-Player in der App + **privater Podcast-RSS-Feed** (Token-URL, widerrufbar).

### 4.5 RAG („Frag deine Inbox“)

- Chunking von Mail-Text und extrahiertem Anhangstext (PDF, DOCX, TXT; später OCR).
- **Hybrid-Retrieval**: Postgres-Volltextsuche (`tsvector`) + pgvector (HNSW), Fusion via Reciprocal Rank Fusion,
  optional Reranker.
- Filter (Zeitraum, Absender, Ordner, Kategorie) werden aus der Frage extrahiert bzw. im UI gesetzt.
- Antworten werden gestreamt (SSE) und enthalten **immer Zitate** mit Links auf die Quell-Mails.
- Strikte Zugriffskontrolle: Retrieval nur über Postfächer, auf die der Nutzer Zugriff hat (Filter in SQL, nicht im Prompt).

## 5. Auth & Mandantenmodell

- **Eine Organisation pro Instanz.** Rollen: `admin`, `user` (erweiterbar, z. B. `auditor`).
- **Bootstrap:** Der erste Login/Registrierung einer frischen Instanz wird Admin. Danach ist die lokale
  Registrierung standardmäßig deaktiviert. Absicherung gegen Race Conditions (DB-Lock) und optional ein
  Setup-Token aus den Logs/Env (`OLLAMAIL_SETUP_TOKEN`).
- **Identity-Provider** (im Admin-UI konfigurierbar, verschlüsselt gespeichert):
  - OIDC generisch + Presets: Microsoft Entra ID, Google, Keycloak/Authentik
  - GitHub (OAuth2), optional eingeschränkt auf Organisationen/Teams
  - LDAP / Active Directory (Bind + Suche, StartTLS/LDAPS, Gruppen)
  - SAML: später
- **Just-in-Time-Provisioning**: Nutzer wird beim ersten Login angelegt; Rollen über Gruppen-Mapping
  (Entra-Gruppen, LDAP-Gruppen, GitHub-Teams); Domain-Allowlist.
- **Sessions**: serverseitig in Postgres, `HttpOnly`/`Secure`/`SameSite=Lax`-Cookie, CSRF-Schutz.
  Keine JWTs im Browser-Storage.
- Optional später: SCIM-Provisioning, TOTP/WebAuthn für lokale Accounts.

### Shared Mailboxes

Ein Postfach gehört entweder einem Nutzer oder ist ein **Shared Mailbox**, das vom Admin angelegt und
Nutzern/Gruppen zugewiesen wird. Zugriffsrechte gelten für alle Features (Triage, Todos, RAG, Digest).

## 6. Frontend

- React 19 + Vite + TypeScript strict, Tailwind v4, shadcn/ui (Radix), TanStack Router (file-based) + Query.
- API-Client generiert aus OpenAPI (`openapi-typescript` + `openapi-fetch`). Das Schema wird ohne
  laufenden Server exportiert (`backend/scripts/export_openapi.py`) und liegt eingecheckt unter
  `frontend/src/api/openapi.json`, die Typen unter `frontend/src/api/schema.gen.ts`. `pnpm gen:api`
  erzeugt beides; die CI schlägt fehl, wenn es nicht zum Backend passt. `operationId` =
  `<Tag>_<Funktionsname>`.
- Fehler: Problem Details → übersetzte Meldung (Toast bei Mutationen, inline bei Queries), 401 →
  Login-Seite. CSRF per Double-Submit (Cookie `ollamail_csrf`, Header `X-CSRF-Token`).
- Echtzeit über SSE (`GET /api/events`): JSON-Events `{"type": "<ressource>.<aktion>", …IDs}`, die
  das Frontend (`useEvents()`) in Query-Invalidierungen übersetzt. Details: `frontend/README.md`.
- i18n (DE/EN), Dark/Light/System, PWA, Tastaturbedienung und Command Palette (⌘K).
- Gestaltung: siehe `docs/DESIGN.md`.

## 7. Betrieb

- Konfiguration per Env (`OLLAMAIL_*`), dokumentiert in `deploy/.env.example`. Start mit Docker Compose: `deploy/README.md`.
- Health-Endpunkte `/healthz` (live) und `/readyz` (DB, Queue, LLM erreichbar).
- Strukturierte JSON-Logs ohne personenbezogene Inhalte; optional OpenTelemetry-Metriken.
- Backups: `pg_dump` + Daten-Volume; Doku in [`OPERATIONS.md`](OPERATIONS.md#5-backup-und-restore).
- Images: `ghcr.io/<owner>/ollamail-{api,frontend}` für `linux/amd64` und `linux/arm64`.

## 8. Architekturentscheidungen (Kurz-ADRs)

| # | Entscheidung | Begründung |
|---|---|---|
| 1 | Python/FastAPI-Backend | Bestes Ökosystem für LLM, RAG, TTS, Mail-Parsing |
| 2 | Postgres + pgvector statt separater Vektor-DB | Ein Datenspeicher, Transaktionen, Hybrid-Suche in SQL, einfache Backups |
| 3 | Procrastinate statt Celery/Redis | Queue in Postgres, async-nativ, weniger Container |
| 4 | Piper als Standard-TTS | Effizient auf CPU/ARM, gute DE/EN-Stimmen, Echtzeit auch ohne GPU |
| 5 | Serverseitige Sessions statt JWT | Widerrufbar, sicherer im Browser, einfacher für SSO-Logout |
| 6 | Eine Organisation pro Instanz | Einfacher, sicherer; Multi-Tenant bei Bedarf später |
| 7 | AGPL-3.0 | Schützt das Self-Hosting-Ökosystem vor geschlossenen SaaS-Forks |
| 8 | Paperless-ngx-Anbindung zurückgestellt | Bewusst nicht im Scope von MVP/v1 |
