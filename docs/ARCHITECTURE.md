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
    providers/   base.py (Interface), imap.py, graph.py (später), gmail.py (später)
    sync/        Initialimport, IDLE/Delta-Sync, Ordner-Mapping
    models.py    Mailbox, Folder, Message, Attachment, Thread
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
class MailProvider(Protocol):
    async def list_folders(self) -> list[RemoteFolder]: ...
    async def fetch_since(self, folder: str, cursor: SyncCursor) -> AsyncIterator[RawMessage]: ...
    async def watch(self, folder: str) -> AsyncIterator[ChangeEvent]: ...  # IMAP IDLE / Graph Webhooks / Gmail Push
    async def move(self, message_ref: str, target_folder: str) -> None: ...
    async def set_flags(self, message_ref: str, flags: set[str]) -> None: ...
    async def apply_label(self, message_ref: str, label: str) -> None: ...  # IMAP: Keyword oder Ordner
```

| Provider | Phase | Auth | Sync | Hinweise |
|---|---|---|---|---|
| IMAP/SMTP | MVP | Passwort/App-Passwort, später XOAUTH2 | UIDVALIDITY/UID + `IDLE`, optional CONDSTORE | Funktioniert mit jedem Server |
| Microsoft 365 | v1 | OAuth2 (Entra ID App, delegiert oder App-only mit Admin-Consent) | Graph Delta Query + Change Notifications | Shared Mailboxes über App-Permissions + `ApplicationAccessPolicy` |
| Gmail / Google Workspace | v1 | OAuth2, Workspace: Domain-wide Delegation | `history.list` + Pub/Sub Push (optional Polling) | Labels statt Ordner |

Postfach-Zugangsdaten und OAuth-Tokens werden verschlüsselt gespeichert (siehe `PRIVACY.md`).

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

### 3.3 TTS

- Interface `TTSEngine.synthesize(text, voice, lang) -> AudioFile`.
- Standard **Piper**: sehr schnell auf CPU (auch ARM), gute deutsche und englische Stimmen, kleine Modelle.
  Weitere Engines (z. B. Kokoro, XTTS) sind als Plugins möglich, falls GPU vorhanden.
- Ausgabe: Opus/MP3, gespeichert im Daten-Volume, Aufbewahrung konfigurierbar.

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
- Backups: `pg_dump` + Daten-Volume; Doku in `docs/OPERATIONS.md` (wird erstellt).
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
