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
    providers/   base.py (Interface), registry.py, fake.py (Tests), imap.py, gmail.py, graph.py (später)
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
  search/        Suchindex: Chunking, Anhangstexte, Embeddings, Hybrid-Suche (RRF)
  rag/           Chat, Zitate (nutzt search/)
  admin/         Instanz-Einstellungen, Auth-Provider, Audit-Log, Statistiken
  audit/         Audit-Log: record(), append-only Tabelle mit Hash-Kette, Admin-API (Liste, CSV)
  worker.py      Procrastinate-App und Task-Registrierung
```

## 3. Zentrale Abstraktionen

### 3.1 Mail-Provider

```python
class MailProvider(Protocol):  # app/mail/providers/base.py
    capabilities: ProviderCapabilities  # labels, push, server_threads, keywords
    async def list_folders(self) -> list[RemoteFolder]: ...
    def fetch_since(self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
                    ) -> AsyncIterator[SyncEvent]: ...  # MessageFetched | MessageUpdated | MessageChanged | MessageDeleted | CursorAdvanced
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
  – je Ordner oder, bei `ProviderCapabilities.mailbox_cursor` (Gmail), postfachweit mit
  `folder_id IS NULL`; dann ruft die Engine `fetch_since(MAILBOX_SCOPE, …)` einmal pro Sync auf.
  `fetch_since` endet immer mit
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

**Besitz:** Ein Postfach gehört genau einem Nutzer (`owner_user_id`, Fremdschlüssel auf `users`
mit `ON DELETE CASCADE`) oder ist shared (`is_shared`, per CHECK erzwungen). Die Zuweisungstabelle
für Shared Mailboxes folgt mit #34.

| Provider | Phase | Auth | Sync | Hinweise |
|---|---|---|---|---|
| IMAP/SMTP | MVP | Passwort/App-Passwort, XOAUTH2 vorbereitet | UIDVALIDITY/UID + `IDLE`, CONDSTORE/QRESYNC falls verfügbar | Funktioniert mit jedem Server |
| Microsoft 365 | v1 (#37) | OAuth2 (Entra ID App, delegiert oder App-only mit Admin-Consent) | Graph Delta Query, Polling; Change Notifications optional | Shared Mailboxes über App-Permissions + RBAC for Applications / `ApplicationAccessPolicy` |
| Gmail / Google Workspace | v1 | OAuth2, Workspace: Domain-wide Delegation | `history.list`, Polling (Standard) oder Pub/Sub-Pull (optional) | Labels statt Ordner; Details: [`providers/gmail.md`](providers/gmail.md) |

Postfach-Zugangsdaten und OAuth-Tokens werden verschlüsselt gespeichert (siehe `PRIVACY.md`;
Spalte `mail_mailboxes.credentials` vom Typ `EncryptedJSON` aus `app/core/crypto.py`).

#### IMAP-Provider (`backend/app/mail/providers/imap*.py`)

- **Client:** eigener schlanker asyncio-Client (`imap_client.py`, Parser in `imap_protocol.py`)
  statt `aioimaplib`: Diese Bibliothek kann kein STARTTLS, loggt Rohdaten (Mail-Inhalte) auf
  DEBUG und überlässt das Parsen dem Aufrufer. Fehler und Logs enthalten nie Server-Texte, nur
  Befehl, Status und Response-Code.
- **Einstellungen** (`provider_settings`, Modell `ImapSettings`): `host`, `port`,
  `security` (`tls` Standard, `starttls`, `none`), `verify_certificate`, `auth`
  (`password` oder `xoauth2`), `username` (Standard: Adresse). Zugangsdaten in `credentials`:
  `password` bzw. `access_token`; für XOAUTH2 kann ein `token_provider` (Token-Refresh, #37/#38)
  übergeben werden. Unverschlüsselte Verbindungen und ungeprüfte Zertifikate lehnt der Provider
  ab, solange der Admin `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS` nicht setzt.
- **Ordner:** `LIST` (mit `RETURN (SPECIAL-USE)`, falls verfügbar). Rollen aus Special-Use-Attributen,
  sonst aus gängigen Namen (DE/EN, nur oberste Ebene). `remote_id` ist der Ordnername wie vom
  Server (modified UTF-7), `name` dekodiert.
- **Referenzen:** `remote_ref = "<UIDVALIDITY>:<UID>:<Ordner>"`.
- **Cursor** pro Ordner: `uidvalidity`, `high` (höchste gesehene UID), `modseq`
  (HIGHESTMODSEQ), `known` (gespeicherte UIDs als kompaktes Sequence-Set) und `import`
  (offener Initialimport: `since`, `below`).
- **Ablauf von `fetch_since`:** (1) Änderungen bekannter Mails – mit QRESYNC ein
  `UID FETCH … (CHANGEDSINCE m VANISHED)`, mit CONDSTORE `CHANGEDSINCE` plus UID-Suche für
  Löschungen, sonst die Flags des bekannten Bereichs; (2) neue Mails (UID > `high`);
  (3) Initialimport: `SINCE` = Zeitraum (Standard 90 Tage), **neueste zuerst**, in Batches
  (`OLLAMAIL_MAIL_SYNC_BATCH_SIZE`), Abruf zusätzlich nach Größe gestückelt (max. 16 MB je
  Roundtrip). Nach jedem Batch kommt `CursorAdvanced`, daher setzt ein abgebrochener Import
  beim letzten Batch fort. Abrufe nutzen `EXAMINE` und `BODY.PEEK[]`, ändern also keine Flags.
- **Push:** `watch()` hält `IDLE` auf einer eigenen Verbindung und erneuert es alle 10 Minuten
  (erkennt auch Verbindungen, die ein NAT-Gateway still getrennt hat).
- **Aktionen:** `set_flags` (`UID STORE FLAGS.SILENT`), `move` (`UID MOVE`, sonst `UID COPY` +
  `UID EXPUNGE`; neue Referenz aus `COPYUID`), `apply_label`/`remove_label` als Keyword
  (Label → gültiges IMAP-Atom: Leerzeichen → `_`, Nicht-ASCII wie Ordnernamen kodiert) oder,
  wenn der Ordner keine Keywords erlaubt (`PERMANENTFLAGS` ohne `\*`), als Kopie in einen Ordner
  mit dem Label-Namen.

#### Microsoft-365-Provider (`backend/app/mail/providers/graph*.py`)

Design, App-Registrierung, Berechtigungsmodelle und Testanleitung:
[`docs/providers/microsoft365.md`](providers/microsoft365.md). Kurzfassung:

- **Auth** (`graph_auth.py`): delegiert per Authorization Code + PKCE (Connect-Flow in
  `graph_router.py`: `POST /api/mail/graph/connect`, `GET /api/mail/graph/callback`) oder
  App-only per Client Credentials für Shared Mailboxes. Entra-App aus `OLLAMAIL_MAIL_GRAPH_*`.
  Tokens werden automatisch erneuert; rotierte Refresh-Tokens speichert der Provider sofort über
  `MailboxConfig.save_credentials`.
- **Client** (`graph_client.py`): unveränderliche IDs (`Prefer: IdType="ImmutableId"`),
  Drosselung mit `Retry-After`, JSON-`$batch`, Fehler nur als Codes.
- **Sync** (`graph.py`): Ordner rekursiv mit Rollen aus Well-known-Namen; Delta Query pro Ordner
  (Cursor = `nextLink`/`deltaLink`), Initialimport mit MIME per `$batch`, danach
  `MessageChanged`; `@removed` wird per `GET` als Verschieben oder Löschen erkannt.
  Kategorien sind Keywords/Labels.
- **Push:** Standard ist Polling. Mit `OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL` hält `watch()` eine
  Subscription aktiv; `POST /api/mail/graph/notifications` (CSRF-frei, `clientState` per HMAC)
  stößt nur `request_sync` an.

#### Gmail-Provider (`backend/app/mail/providers/gmail*.py`)

Design und Einrichtung: [`providers/gmail.md`](providers/gmail.md). Kurzfassung:

- **REST statt IMAP** (`gmail_api.py`: `httpx`, Bearer-Token, Retries für 429/5xx/Rate-Limit-403
  mit `Retry-After`, Multipart-Batch-Requests für `format=raw`). Kein Google-SDK.
- **Tokens** (`gmail_auth.py`): OAuth mit Refresh-Token (verschlüsselt in `credentials`) oder
  Service Account mit Domain-wide Delegation (JWT RS256, Schlüssel per
  `OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE`). Access-Tokens nur im Prozessspeicher.
- **Connect-Flow** (`gmail_connect.py`): `POST /mail/gmail/oauth/start` und
  `GET /mail/gmail/oauth/callback` (PKCE, `state` in signiertem `HttpOnly`-Cookie); legt das
  Postfach des angemeldeten Nutzers an oder erneuert dessen Refresh-Token.
- **Labels ↔ Ordner:** System-Labels (INBOX, SENT, DRAFT, SPAM, TRASH), Nutzer-Labels und der
  virtuelle Ordner `ALL_MAIL` („Alle Nachrichten“); `UNREAD`/`STARRED` werden zu Flags.
- **Sync** mit postfachweitem Cursor (`historyId`): Initialimport über `messages.list`, danach
  `history.list`; abgelaufene History → `CursorInvalidError` → Resync mit Abgleich.
- **Push:** Pub/Sub *Pull* (keine öffentliche URL); ohne Konfiguration Polling.

#### Sync (`backend/app/mail/sync/`)

- **`engine.sync_mailbox`** (providerunabhängig): Ordnerliste spiegeln (neue Ordner, auf dem
  Server gelöschte Ordner samt Mails löschen; Ordner mit ausgeschlossener Rolle aus
  `SyncSettings.excluded_roles` – Standard Papierkorb und Spam – werden angelegt, aber nicht
  synchronisiert), dann je Ordner (INBOX zuerst) `fetch_since` anwenden: `MessageFetched` →
  `store_message`, `MessageUpdated` → Flags/Ordner, `MessageDeleted` → `delete_messages`,
  `MessageChanged` (Provider kann neu/geändert nicht unterscheiden, z. B. Graph Delta) → bekannt:
  wie `MessageUpdated`, unbekannt: Quelle über `event.load()` laden und speichern. Liegt eine Mail
  in keinem synchronisierten Ordner mehr (z. B. in den Papierkorb verschoben), wird sie gelöscht.
  Provider können erneuerte Zugangsdaten über `MailboxConfig.save_credentials` speichern (eigene
  Transaktion, unabhängig vom Sync-Commit). Bei jedem
  `CursorAdvanced` werden Mails, Änderungen und Cursor **in einer Transaktion** committet.
  Ungültiger Cursor → Mails des Ordners löschen und neu importieren.
- **Postfachweiter Cursor** (`capabilities.mailbox_cursor`, Gmail): ein `fetch_since` pro Sync,
  Cursor in der Zeile mit `folder_id IS NULL`. Gespeichert werden nur Mails, die in mindestens
  einem synchronisierten Ordner liegen; verlässt eine Mail den letzten, wird sie gelöscht. Die
  Referenzen sind stabil, deshalb löscht ein ungültiger Cursor nicht alles: Der Zeitraum wird neu
  importiert (bekannte Mails werden nur aktualisiert, Hooks feuern nur für neue), danach werden
  Mails des Zeitraums gelöscht, die der Server nicht mehr geliefert hat. Der Cursor wird dabei
  erst am Ende gespeichert, ein abgebrochener Resync beginnt neu.
- **Status:** `SyncState.last_error` (nur Fehlercodes) und `last_synced_at` je Ordner; Fehler, die
  das ganze Postfach betreffen (Anmeldung, Verbindung, Konfiguration), und der letzte vollständige
  Sync stehen in der Zeile mit `folder_id IS NULL`. Der Besitzer erhält Events `mailbox.sync`
  (`progress`, `done`, `failed`).
- **Job** `mail.sync_mailbox` (Queue `sync`, `lock` und `queueing_lock` pro Postfach); anstoßen mit
  `app.mail.sync.tasks.request_sync(mailbox_id)`. Verbindungsfehler lösen Retries aus,
  Anmelde- und Konfigurationsfehler nicht.
- **Watcher** (`watcher.py`): läuft im Worker-Prozess neben den Job-Workern (kein Job, damit er
  keinen Worker-Slot dauerhaft belegt). Pro Postfach eine `IDLE`-Verbindung; jedes Push-Event
  stößt einen Sync an, zusätzlich alle `poll_interval_seconds` (andere Ordner, Server ohne
  `IDLE`). Reconnect mit Backoff. Mehrere Worker teilen sich die Postfächer über
  PostgreSQL-Advisory-Locks; stirbt ein Worker, übernimmt ein anderer. Abschaltbar mit
  `OLLAMAIL_MAIL_WATCH_ENABLED=false` (dann gibt es keine automatischen Syncs).
- **Hook für neue Mails** (`app/mail/hooks.py`): Nach dem Commit ruft der Sync für jede *neue*
  Mail `message_stored(mailbox_id, message_id, backfill=...)` auf. Handler registrieren sich mit
  `@on_message_stored` (siehe 4.1, die Verarbeitungspipeline tut das); ein fehlschlagender
  Handler wird geloggt und stoppt den Sync nicht.

#### Postfach-API (`backend/app/mail/api/`)

Nutzer verwalten ihre eigenen Postfächer unter `/mailboxes`. Die API nutzt Registry, Sync-Job
und `delete_mailbox`; sie baut nichts davon nach.

| Endpunkt | Zweck |
|---|---|
| `POST /mailboxes/autodiscover` | Host/Port-Vorschläge zur Adresse (bekannte Anbieter, sonst `imap.<domain>`/`mail.<domain>`). Offline, keine DNS-/HTTP-Abfragen; Adresse im Body, damit sie nicht in Access-Logs landet. Hinweise als Codes (`app_password`, `enable_imap`, `oauth_required`, …) |
| `POST /mailboxes/test` | Verbindungstest ohne Speichern (Provider aus der Registry, `list_folders`). Ergebnis `ok`, Fehlercode oder Ordnerliste für die Ordnerauswahl vor dem Anlegen |
| `GET/POST /mailboxes` | Eigene Postfächer mit Sync-Status; Anlegen testet die Verbindung (422 mit `error_code`), lehnt Duplikate ab (409) und stößt den Initialimport an |
| `GET/PATCH/DELETE /mailboxes/{id}` | Details; Umbenennen, Verbindung/Zugangsdaten (vor dem Speichern getestet), Importzeitraum, ausgeschlossene Rollen, Pausieren/Fortsetzen (`sync_enabled`); Entfernen |
| `GET /mailboxes/{id}/status` | Nur der Sync-Status |
| `POST /mailboxes/{id}/sync` | Sync sofort anstoßen (202, `queued`); 409, wenn pausiert |
| `GET/PATCH /mailboxes/{id}/folders` | Ordner mit Auswahl und Status je Ordner; Auswahl setzen |
| `GET /mailboxes/providers` | Postfachtypen, die sich auf dieser Instanz anlegen lassen: `credentials` (Formular, z. B. IMAP) oder `oauth` mit `oauth_start_path`. OAuth-Typen erscheinen nur, wenn der Provider registriert und sein OAuth-Client konfiguriert ist (`app/mail/api/providers.py`, ein Eintrag je Provider) |

- **Zugriff:** ausschließlich über `app/mail/api/access.py` (`get_mailbox`, `visible_to`,
  Berechtigungen `read`/`sync`/`manage`). Heute nur der Besitzer; #34 erweitert diese Funktionen
  um Shared Mailboxes. Fremde Postfächer verhalten sich wie nicht vorhandene (404).
- **Zugangsdaten** sind write-only (Antworten enthalten nur `has_credentials`) und werden
  verschlüsselt gespeichert. Ein PATCH ersetzt sie als Ganzes; neue Verbindungsdaten werden mit den
  gespeicherten Zugangsdaten getestet.
- **Sync-Status** (`MailboxSyncStatus`): `phase` = `paused` | `error` (letzter Sync für das ganze
  Postfach fehlgeschlagen) | `syncing` (Sync-Job wartet oder läuft, aus `procrastinate_jobs`) |
  `pending` (nie synchronisiert) | `importing` (Initialimport eines Ordners offen) | `idle`, dazu
  letzte Synchronisierung, Fehlercode, Ordner gesamt/importiert/fehlgeschlagen und Anzahl Mails.
  „Import offen“ heißt: kein Cursor oder der Cursor enthält den Schlüssel `import`
  (Konvention für Provider, die in Batches importieren, siehe `SyncCursor`).
- **Ordnerauswahl** setzt `Folder.sync_enabled` und hält `SyncSettings.excluded_folders`
  synchron (bleibt erhalten, wenn ein Ordner neu angelegt wird). Abgewählte Ordner behalten ihre
  gespeicherten Mails. Ordner mit ausgeschlossener Rolle (Papierkorb, Spam) bleiben aus, bis die
  Rolle aus `excluded_roles` entfernt wird. Der Importzeitraum gilt für Ordner, deren Import noch
  nicht begonnen hat.
- **Events:** Neben `mailbox.sync` aus dem Sync sendet die API `mailbox.changed`
  (`created`, `updated`, `deleted`) an den Besitzer.
- **Jobs aus der API:** `app/core/jobs.py` öffnet die Procrastinate-App beim ersten Einreihen
  (der Start der API hängt nicht an der Queue) und schließt sie beim Shutdown.
- **Audit:** `mailbox.created` und `mailbox.deleted` (über `delete_mailbox`) mit dem Nutzer als
  Akteur, in derselben Transaktion wie die Änderung.

#### Mail-Lese-API (`backend/app/mail/api/messages.py`)

Grundlage der Inbox (#16). Zugriff wie bei der Postfach-API über `access.visible_to`: Mails fremder
Postfächer antworten 404.

| Endpunkt | Zweck |
|---|---|
| `GET /messages` | Eine Zeile je Mail, neueste zuerst, Keyset-Paging (`cursor`, `limit` ≤ 200), `total` für die virtualisierte Liste. Filter: `mailbox_id`, `folder_id` (ohne: Ordner mit Rolle `inbox`), `unread`. Ohne Bodies; nur ein Snippet aus `body_main` |
| `GET /messages/{id}/thread` | Konversation der Mail, älteste zuerst (höchstens die neuesten 100), mit Empfängern, Text, sanitisiertem HTML (`body.html`, `body.blocked_images`) und Anhängen |
| `GET /messages/{id}/body?external_images=true` | HTML mit externen Bildern – erst, wenn der Nutzer sie für diese Mail anfordert |
| `PATCH /messages/{id}` | `{"seen": bool}`: gelesen/ungelesen. Sofort gespeichert, Event `message.updated`, Job `mail.write_flags` schreibt die Flags auf den Server |
| `GET /messages/{id}/attachments/{attachment_id}` | Download (`Content-Disposition: attachment`, `application/octet-stream`, `nosniff`, CSP `sandbox`). `?inline=true` nur für PNG/JPEG/GIF/WebP (`cid:`-Bilder im HTML) |

- **HTML:** immer serverseitig mit `sanitize_html` bereinigt; das Roh-HTML verlässt den Server nie.
  `cid:`-Bilder zeigen auf `/api/messages/{id}/attachments/{aid}?inline=true`.
- **Gelesen/ungelesen:** Quelle ist der gespeicherte Flag-Satz. `mail.write_flags` (Queue `sync`,
  Lock je Mail) schreibt beim Ausführen den aktuellen Stand per `MailProvider.set_flags`; dauerhafte
  Fehler (Mail weg, nur Lesezugriff, Anmeldung) werden als Code geloggt und verworfen,
  Verbindungsfehler wiederholt.
- **Anzeige im Frontend:** sandboxed `iframe` (`srcdoc`, ohne `allow-scripts`) mit eigener CSP
  (`default-src 'none'`, Bilder nur `'self'`/`data:` bis zum Klick auf „Bilder laden“), siehe
  `frontend/README.md`.

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
  config.py         LLMConfigResolver (Protocol), ResolvedConfig (Env + Admin-Overrides), EnvConfigResolver
  limiter.py        zur Laufzeit änderbare Parallelität (Worker)
  profiles.py       Hardware-Profile cpu / gpu-consumer / gpu-server
  structured.py     Pydantic → JSON-Schema, Validierung, Retry, Prompt-Fallback
  context.py        Token-Schätzung, Kürzen langer Mails
  metrics.py        LLMCallMetrics + MetricsSink (Standard: Log-Event `llm_call`)
  gateway.py        LLMGateway – einziger Einstiegspunkt für Features
ai/prompts/         versionierte, sprachabhängige Prompt-Templates (`name@version`)
ai/settings/        KI-Einstellungen in der DB (#18): Modelle, Store, DbConfigResolver, Admin-API
```

Features nutzen ausschließlich das `LLMGateway` (`app.state.llm`, FastAPI-Dependency `get_llm`):

```python
triage = await llm.complete_structured(
    LLMTask.TRIAGE, prompt.render(mail_language, mail=text), TriageResult,
    prompt_version=prompt.id, language=mail_language,
)
```

Das Gateway erledigt pro Aufruf:

1. **Auflösung** Task → Endpunkt + Modell über einen `LLMConfigResolver`. API und Worker nutzen
   `DbConfigResolver` (Admin-Einstellungen, siehe unten); `EnvConfigResolver` liest nur
   Umgebungsvariablen (Evaluation, Tests). Gateway und Features kennen den Unterschied nicht.
   Reihenfolge Modell: Admin-Zuordnung des Tasks → `OLLAMAIL_LLM_TASK_<TASK>_MODEL` →
   `OLLAMAIL_LLM_DEFAULT_{CHAT,EMBEDDING}_MODEL` → Profil (Admin-Wahl, sonst `OLLAMAIL_LLM_PROFILE`).
   Endpunkt: Admin-Zuordnung → `OLLAMAIL_LLM_TASK_<TASK>_ENDPOINT` → `default`.
2. **Cloud-Sperre:** Endpunkte mit `is_cloud` werden nur genutzt, wenn Cloud-LLMs erlaubt sind
   (Admin-Schalter, Standard `OLLAMAIL_LLM_CLOUD_ENABLED=false`). Sonst `CloudLLMDisabledError`,
   bevor ein HTTP-Client entsteht.
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

#### KI-Einstellungen im Admin-Bereich (`backend/app/ai/settings/`)

- **Speicher:** `ai_providers` (weitere Endpunkte; API-Key als `EncryptedStr`) und die einzeilige
  Tabelle `ai_settings` (Cloud-Schalter, Profil, Parallelität, Zuordnung `{task: {provider, model}}`).
  `NULL` bzw. ein fehlender Task heißt: Wert aus der Umgebung. Endpunkte aus der Umgebung
  (`default`, `OLLAMAIL_LLM_ENDPOINTS`) erscheinen schreibgeschützt und gewinnen bei Namensgleichheit.
- **Ohne Neustart:** Jeder Prozess cacht einen Snapshot (`DbConfigResolver`). Jede Änderung sendet
  im selben Commit `NOTIFY ollamail_ai_settings`; jeder API- und Worker-Prozess hört per `LISTEN`
  und verwirft seinen Snapshot. Fallback ohne Benachrichtigung: Snapshot höchstens 30 s alt. Der
  nächste Job nutzt damit das neue Modell. Ist die DB nicht lesbar, bleibt der letzte Snapshot;
  ohne Snapshot gelten die Umgebungswerte **mit gesperrter Cloud** (fail closed).
- **Worker:** Alle Jobs eines Prozesses teilen ein Gateway (`app.ai.settings.runtime.worker_gateway`).
  Die Parallelität (Admin, 1 bis `OLLAMAIL_LLM_MAX_CONCURRENCY`) begrenzt gleichzeitige
  LLM-Anfragen im Gateway (`limiter.py`); die Job-Slots der `llm`-Queue sind die Obergrenze.
- **Admin-API** (`/api/admin/ai`, nur Admins): `GET/PATCH /settings`, `GET/POST /providers`,
  `PATCH/DELETE /providers/{name}`, `POST /providers/{name}/test` und `POST /providers/test`
  (ungespeicherte Werte; ohne Key wird der gespeicherte genutzt). Der Verbindungstest ruft nur die
  Modellliste ab, es gehen keine Mail-Inhalte hinaus. API-Keys sind write-only (`api_key_set`).
  Ein Provider, dem Tasks zugeordnet sind, lässt sich nicht löschen (409).
- **Nutzer:** `GET /api/ai/status` listet Cloud-Provider, die gerade Mail-Inhalte erhalten, je Task.
  Die UI zeigt das dauerhaft und dezent über dem Inhalt an (`docs/PRIVACY.md`).
- **Audit:** jede Änderung als `ai.settings_changed` (`change`, `provider`, `is_cloud`,
  `cloud_enabled`, `profile`, `concurrency`, `tasks`; nie API-Keys).

**Profil-Defaults** (`profiles.py`). Die Modellnamen sind **Beispiele** und lassen sich per Env
überschreiben:

| Profil | Chat-Modell (Beispiel) | Embeddings (Beispiel) | Kontext |
|---|---|---|---|
| `cpu` (Standard) | `qwen2.5:3b` | `bge-m3` | 8192 |
| `gpu-consumer` | `qwen2.5:14b` | `bge-m3` | 16384 |
| `gpu-server` | `qwen2.5:32b` | `bge-m3` | 32768 |

### 3.3 TTS

Umgesetzt in `backend/app/ai/tts/`.

- Einstieg für Features: `TTSService.synthesize(text, lang=..., voice=..., target=..., formats=...)
  -> list[AudioFile]` (`get_tts()`), aufgerufen aus Jobs der Queue `tts`, nie im Request.
  Der Service wählt die Stimme (Nutzerwahl, falls sie zur Sprache passt, sonst Standard je Sprache),
  normalisiert und segmentiert den Text, lässt ihn stückweise sprechen, fügt Pausen ein und streamt
  das PCM in ffmpeg.
- Interface `TTSEngine.synthesize(text, voice, lang) -> PCMAudio` für **ein** kurzes, bereits
  normalisiertes Stück, dazu Stimmenverwaltung (`ensure_voice`, `installed_voices`). Normalisierung,
  Pausen und Encoding bekommen neue Engines dadurch geschenkt.
- **Registry** (`registry.py`): `OLLAMAIL_TTS_ENGINE` wählt die Engine. Weitere Engines (z. B. Kokoro,
  XTTS bei vorhandener GPU) per `register_engine` oder als Plugin-Paket mit Entry Point
  `ollamail.tts_engines`.
- Standard **Piper** (`piper-tts`, ONNX Runtime, in-process): sehr schnell auf CPU (auch ARM), gute
  deutsche und englische Stimmen, kleine Modelle. Synthese pro Prozess serialisiert (espeak-ng ist
  nicht threadsicher, ONNX Runtime nutzt ohnehin alle Kerne).
- **Stimmen** liegen im Daten-Volume (`<data_dir>/tts/voices/piper/`), nicht im Image. Fehlende werden
  von `OLLAMAIL_TTS_VOICE_BASE_URL` geladen (abschaltbar, dann manuell kopieren). Stimmen-IDs werden
  per Muster validiert (`de_DE-thorsten-medium`), die Sprache ist Teil der ID.
- **Text-Normalisierung** (`normalize.py`, DE/EN): Datumsangaben, Uhrzeiten, Beträge, Prozente,
  Zahlen (Jahre, Dezimal-/Tausendertrennzeichen je Sprache, Telefonnummern ziffernweise),
  Abkürzungen, Links, E-Mail-Adressen, Markdown und Emojis; danach Satz- und Absatzsegmentierung und
  Aufteilung langer Sätze an Satzteilgrenzen (`OLLAMAIL_TTS_MAX_CHUNK_CHARS`).
- Ausgabe: Opus (Standard) und MP3 (Podcast-Apps) in einem ffmpeg-Lauf, atomar geschrieben
  (`.part` → Umbenennung). Gespeichert im Daten-Volume, Aufbewahrung konfigurierbar (#28/#36).
- Logs enthalten nur Stimme, Sprache, Längen und Zeiten, nie den Text.

### 3.4 Hintergrundjobs & Echtzeit-Events

Umgesetzt in `backend/app/worker.py` und `backend/app/core/events.py`.

- **Procrastinate** mit Postgres als Queue. Das Schema ist eine Alembic-Migration (vendored SQL in
  `backend/migrations/sql/`); Autogenerate ignoriert die `procrastinate_*`-Tabellen.
  Ein Procrastinate-Update mit Schemaänderung braucht eine neue Migration – ein Test schlägt sonst an.
- **Queues** `sync`, `llm`, `tts`, `default`. `OLLAMAIL_WORKER_QUEUES` wählt die Queues eines
  Worker-Prozesses; `llm` hat eigene Job-Slots (`OLLAMAIL_LLM_MAX_CONCURRENCY`), von denen das
  LLM-Gateway höchstens die im Admin-Bereich eingestellte Parallelität (Standard
  `OLLAMAIL_LLM_CONCURRENCY`) gleichzeitig an das Modell lässt; alle anderen Queues teilen sich
  `OLLAMAIL_WORKER_CONCURRENCY`.
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
- **Aktueller Nutzer:** Dependency `app.core.current_user.get_current_user_id` (Session-Cookie,
  siehe §5); ohne gültige Session 401. Tests überschreiben sie.

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

**Schnittstelle zum Sync** (`app/mail/hooks.py`): Der Sync ruft nach dem Commit für jede *neue*
Mail `message_stored(mailbox_id, message_id, backfill=...)` auf (nicht bei Flag-Änderungen oder
erneutem Abruf); `backfill` ist wahr für Mails aus dem Initialimport. `app.processing.tasks`
registriert sich dort mit `@on_message_stored` und ruft `enqueue_processing` mit
`Priority.BACKFILL` bzw. `Priority.NEW` auf. Weitere Features können sich ebenso einhängen.

**Umsetzung (`backend/app/processing/`, #19):**

- **Einstieg:** `await enqueue_processing(message_id, priority=Priority.NEW)` aus
  `app.processing.tasks` – vom Mail-Sync **nach dem Commit** der Mail aufzurufen; beim Erstimport
  mit `Priority.BACKFILL`. Mehrfache Aufrufe sind unschädlich.
- **Schritte** registrieren sich in ihrem Feature-Modul (das in `TASK_MODULES` steht):
  `@registry.step("triage", version=1, queue="llm", depends_on=("normalize",))`. Ein Handler
  bekommt `StepContext(session, message_id, mailbox_id)`, schreibt über die Session und committet
  nicht – Ergebnis und Status werden gemeinsam committet. Handler müssen idempotent sein.
- **Optionale Vorgänger:** `after=("triage",)` ordnet einen Schritt hinter einen anderen, ohne ihn
  vorauszusetzen. Ist der Vorgänger registriert, wartet der Schritt, bis er `done` **oder** `failed`
  ist; ist er nicht registriert, wird er ignoriert. So laufen Todos nach der Triage, wenn es sie
  gibt, und ohne Triage-Ergebnis sonst trotzdem. `depends_on` bleibt die harte Abhängigkeit.
- **Ablauf:** `processing.plan_message` (Queue `default`) legt je Schritt eine Zeile in
  `message_processing` an (`pending`) und reiht die Schritte ohne offene Abhängigkeiten als
  `processing.run_step` in der Queue des Schritts ein. Ist ein Schritt `done`, werden seine
  Nachfolger eingereiht, sobald alle ihre Abhängigkeiten `done` sind. Locks je Mail und Schritt
  verhindern doppelte Jobs; ein bereits erledigter Schritt wird übersprungen.
- **Status:** `pending` → `running` → `done` bzw. `failed`. Ein fehlgeschlagener Versuch, der
  wiederholt wird, steht wieder auf `pending` (mit Fehlercode); `failed` heißt „aufgegeben“
  (Retries erschöpft oder `StepError(code, permanent=True)`). Gespeichert werden nur Fehlercodes.
- **Versionen:** Erhöht ein Schritt seine `version` (z. B. neuer Prompt), reiht der periodische Job
  `processing.requeue_outdated` (alle 10 Minuten, `OLLAMAIL_PROCESSING_REQUEUE_BATCH_SIZE` Mails,
  neueste zuerst) die betroffenen Mails ein; nur dieser Schritt läuft erneut. Derselbe Job holt Mails
  nach, die nie verarbeitet wurden.
- **Priorität:** Alle Jobs einer Mail erben die Priorität. Worker nehmen immer den Job mit der
  höchsten Priorität: `NEW` (10) vor `BACKFILL` (0) vor `REPROCESS` (−10). Ein Erstimport blockiert
  neue Mails also höchstens für die Dauer eines laufenden Jobs.
- **Events:** `message.processed` mit `message_id`, `mailbox_id` und Status `done` (alle Schritte
  erledigt) bzw. `failed`, an den Besitzer des Postfachs.
- **Abschalten:** global `OLLAMAIL_PROCESSING_ENABLED=false`, je Postfach
  `python -m app.cli processing disable|enable <mailbox-id>` (Tabelle `processing_mailbox_settings`).
- **Neu verarbeiten:** `python -m app.cli processing reprocess [--mailbox ID] [--since …] [--until …]
  [--step NAME …]` setzt die gewählten Schritte auf `pending` und reiht die Mails mit `REPROCESS` ein,
  auch wenn sie schon `done` oder `failed` waren. Abhängige Schritte laufen dabei nicht automatisch
  mit. Der Befehl hilft auch, falls Jobs verloren gingen (z. B. Absturz zwischen Commit und Einreihen).

### 4.2 Triage

- Standardkategorien: **Wichtig**, **Handlungsbedarf**, **Warten auf**, **Info**, **Newsletter**, **Benachrichtigung**, **Spam/Werbung**.
- Nutzer (und Admin als Org-Default) können Kategorien mit Beschreibung in natürlicher Sprache anlegen.
- Korrekturen durch den Nutzer werden gespeichert und als Few-Shot-Beispiele genutzt (pro Nutzer, nie nutzerübergreifend).
- Vorfilter ohne LLM (Header wie `List-Unsubscribe`, `Precedence: bulk`, bekannte Absender) sparen Rechenzeit.

**Umsetzung (`backend/app/triage/`, #20):**

- **Kategorien** (`categories.py`, `models.py`): Die sieben Standardkategorien legt die Migration als
  Org-Kategorien an (`owner_user_id IS NULL`, `builtin_key` für die Übersetzung im UI). Admins pflegen
  Org-Kategorien (`/triage/organization/categories`), Nutzer legen eigene an und blenden beliebige aus
  bzw. sortieren sie (`triage_category_preferences`). Ausgeblendete Kategorien bietet die Triage dem
  Modell nicht an; mindestens eine bleibt sichtbar.
- **Pipeline:** Schritt `triage` (Queue `llm`, `TRIAGE_STEP_VERSION`), danach `triage_write_back`
  (Queue `sync`). Eine Korrektur des Nutzers (`source = user`) überschreibt die Triage nie, auch nicht
  beim Neuverarbeiten.
- **Todos:** Der Schritt `todos` läuft `after=("triage",)` und liest die Kategorie über
  `app.triage.service.category_key` (`builtin_key` bzw. Slug des Namens, registriert beim Import von
  `app.triage.tasks`). Mails in `OLLAMAIL_TODOS_SKIP_CATEGORIES` (Standard: Newsletter,
  Benachrichtigung, Spam) werden nicht nach Todos durchsucht.
- **Vorfilter** (`rules.py`, ohne LLM, `OLLAMAIL_TRIAGE_PREFILTER_ENABLED`): erst Absenderregeln des
  Nutzers (Adresse vor Domain), dann `Auto-Submitted` ≠ `no` und Roboter-Absender (`no-reply@`, …) →
  Benachrichtigung, `Precedence: junk` → Spam, `List-Unsubscribe`/`Precedence: bulk|list` → Newsletter,
  Priorität 3. Gespeichert wird der Regelname (`rule`), keine Begründung.
- **LLM** (`classify.py`, Prompt `triage@1` in `prompts.py`): `LLMGateway.complete_structured` mit
  Task `triage`, Temperatur 0. Das Antwortschema wird je Aufruf gebaut, die erlaubten
  Kategorie-Schlüssel stehen als `enum` darin. Ergebnis: Kategorie, Priorität 1–3 (1 = hoch), ein Satz
  Begründung in der UI-Sprache des Nutzers. Die Mail steht als Daten zwischen `<<<`/`>>>`, Text gekürzt
  auf `OLLAMAIL_TRIAGE_MAX_BODY_CHARS`.
- **Lernen aus Korrekturen** (`feedback.py`): `PUT /triage/messages/{id}` speichert die Korrektur als
  Ergebnis und als Beispiel (`triage_feedback`). In den Prompt kommen bis zu
  `OLLAMAIL_TRIAGE_FEW_SHOT_EXAMPLES` Beispiele **nur desselben Nutzers** (Filter auf Nutzer *und* auf
  eigene Postfächer). Gibt es mehr Kandidaten, wählt die Triage die ähnlichsten per Embedding
  (Kosinus, Embeddings werden im Job nachberechnet und mit Modellname gespeichert); ohne
  Embedding-Modell die neuesten. Absenderregeln schlägt `GET /triage/sender-rules/suggestions` vor,
  sobald ein Absender mindestens `OLLAMAIL_TRIAGE_RULE_SUGGESTION_MIN_CORRECTIONS`-mal und immer in
  dieselbe Kategorie korrigiert wurde.
- **Zurückschreiben** (`writeback.py`, opt-in je Postfach, `PUT /triage/mailboxes/{id}/settings`):
  `label` ruft `MailProvider.apply_label` mit `<prefix><key>` auf (IMAP-Keyword, Gmail-Label,
  Graph-Kategorie) und entfernt vorher das alte Label; `move` verschiebt mit `MailProvider.move` in
  einen vorhandenen Ordner dieses Namens. Neue Mails erledigt der Pipeline-Schritt; Korrekturen und
  das nachträgliche Aktivieren arbeitet der minütliche Job `triage.write_back` ab
  (`write_back_pending`).
- **API** (`/triage`): Kategorien (CRUD, Reihenfolge, Ausblenden), Triage einer Mail lesen/korrigieren,
  Inbox nach Kategorie gruppiert (`GET /triage/inbox`, Posteingangsordner der eigenen Postfächer),
  Absenderregeln, Write-back-Einstellung je Postfach.
- **Evaluierung:** `uv run python -m scripts.eval_triage --model qwen2.5:3b [--model …]` klassifiziert
  einen synthetischen, gelabelten Datensatz (`scripts/triage_eval_dataset.json`, DE/EN) und gibt die
  Genauigkeit je Modell aus (Kategorie, Priorität, Anteil Vorfilter, Fehlklassifikationen).

### 4.3 Todos

- Extraktion: Titel, Beschreibung, Fälligkeit (falls genannt), Priorität, Link zur Quell-Mail/zum Thread.
- Status: offen / erledigt / verworfen. Duplikaterkennung innerhalb eines Threads.

**Umsetzung (`backend/app/todos/`, #22):**

- **Schritt** `todos` (`steps.py`, Queue `llm`, `after=("triage",)`): läuft nicht für Mails, deren
  Triage-Kategorie in `OLLAMAIL_TODOS_SKIP_CATEGORIES` steht (Standard `newsletter,notification,spam`).
  Ohne Triage-Ergebnis (Triage nicht installiert, fehlgeschlagen) läuft er immer. Die Kategorie liest
  `extraction.message_category`; die Triage (#20) installiert ihren Lookup mit
  `extraction.set_category_lookup(...)`. Geteilte Postfächer werden bis #34 übersprungen.
  `OLLAMAIL_TODOS_EXTRACTION_ENABLED=false` schaltet den Schritt ab.
- **Prompt** `todos_extract@1` (`app/ai/prompts/todos.py`, DE/EN) über `LLMGateway.complete_structured`
  mit `LLMTask.TODOS`. Das Modell bekommt Absender, Empfänger, Betreff, Text ohne Zitate, das
  Sendedatum (Wochentag + Datum in der Zeitzone des Nutzers), ob der Nutzer die Mail selbst
  geschrieben hat, und die offenen Todos des Threads (nummeriert).
- **Antwort** (`TodoExtraction`): je Aufgabe Titel, Beschreibung, `due_phrase` (Frist wörtlich aus
  der Mail), `due_date` (Schätzung des Modells), Priorität `high|normal|low`, Konfidenz und optional
  `updates` (Nummer eines offenen Todos); dazu `done` (Nummern erledigter Todos). Zu lange Texte werden
  gekürzt, Konfidenz und Priorität normalisiert, statt die Antwort zu verwerfen.
- **Nachbearbeitung** (`plan_extraction`, ohne DB):
  - Aufgaben unter `OLLAMAIL_TODOS_MIN_CONFIDENCE` werden verworfen.
  - Mails, die der Nutzer selbst geschrieben hat (Absender = Postfachadresse), erzeugen keine neuen
    Todos, können aber „erledigt“ vorschlagen.
  - **Fälligkeit** (`dates.py`): Die Frist wird deterministisch aus `due_phrase` berechnet,
    Bezugstag ist das Sendedatum in der Zeitzone des Nutzers. Die Schätzung des Modells gilt nur,
    wenn die Phrase unbekannt ist, und nur, wenn sie nicht vor dem Bezugstag und höchstens zwei
    Jahre danach liegt. Bei Mehrdeutigkeit gilt das frühere Datum: „nächsten Freitag“ ist der
    nächste Freitag nach dem Bezugstag, „Freitag nächster Woche“ der Freitag der Folgewoche.
  - **Duplikate:** Verweist eine Aufgabe auf ein offenes Todo des Threads (`updates`) oder hat sie
    denselben Titel, wird das Todo aktualisiert statt neu angelegt. Vom Nutzer bearbeitete Todos
    (`is_edited`) behalten Titel, Beschreibung und Fälligkeit.
  - **„Erledigt“** wird nur vorgeschlagen (`done_suggested`); der Status bleibt `open`.
- **Idempotenz:** Ein erneuter Lauf für dieselbe Mail löscht zuerst die Todos, die ein früherer Lauf
  aus ihr erzeugt hat, sofern der Nutzer sie nicht angefasst hat (offen, nicht bearbeitet, kein
  Vorschlag).
- **Modell** `Todo` (`models.py`, Tabelle `todos`): Nutzer, Quelle (Postfach, Mail, Thread),
  Status `open|done|dismissed`, `is_manual`, `is_edited`, Konfidenz, `done_suggested`,
  `completed_at` und `external_refs` (JSON, für den Export in #40).
- **API** (`/todos`, nur eigene Todos): `GET /todos` (Filter `status` mehrfach, `mailbox_id`,
  `due_before`, `due_after`; früheste Fälligkeit zuerst, ohne Fälligkeit zuletzt; `limit`/`offset`),
  `POST /todos` (manuell, optional mit `message_id` einer eigenen Mail), `GET|PATCH|DELETE /todos/{id}`.
  `PATCH` bearbeitet Felder und den Status (`done` setzt `completed_at`, jede Statusänderung löscht
  den Vorschlag); `done_suggested: false` verwirft nur den Vorschlag.
- **Evaluierung:** `python -m app.todos.evaluation [--model NAME ...]` läuft mit dem echten Prompt
  gegen den konfigurierten Endpunkt über `app/todos/eval_cases.json` (synthetische DE/EN-Mails mit
  erwarteten Todos, Fristen, Updates und Erledigt-Vorschlägen) und gibt je Modell Precision, Recall
  und Trefferquote der Fristen aus. Neue Fälle im selben Format ergänzen.
- **Ziel (dokumentiert, später):** Export/Sync via CalDAV (VTODO), Microsoft To Do (Graph), Google Tasks.

### 4.4 Daily Digest (Audio)

- Periodischer Job pro Nutzer zur eingestellten Uhrzeit und Zeitzone.
- Zusammenfassung der Mails seit dem letzten Digest: wichtige Mails, offene Todos, Termine/Fristen.
- Text → Piper → Audiodatei. Text-Version wird mitgespeichert (Transkript, barrierefrei).
- Auslieferung: Web-Player in der App + **privater Podcast-RSS-Feed** (Token-URL, widerrufbar).

**Umsetzung (`backend/app/digest/`, #28):**

- **Einstellungen pro Nutzer** (`digest_user_settings`, API `GET/PATCH /api/digests/settings`):
  aktiv, Uhrzeit, Zeitzone (leer = Profil), Wochentage (0 = Montag), Sprache (`de`/`en`, leer =
  Profil), Stimme (leer = Standardstimme der Sprache), Länge (`short`/`normal`), Postfächer (leer =
  alle lesbaren). Ohne Zeile gelten die Standards, der Digest ist aus.
- **Zeitplan** (`app.digest.schedule`, reine Funktionen): Der periodische Job `digest.schedule`
  (jede Minute, Queue `default`) berechnet je Nutzer den letzten fälligen Termin in dessen
  Zeitzone und legt dafür genau einen Digest an (`last_scheduled_for` plus Unique-Constraint
  `(user_id, scheduled_for)`). Sommerzeit nach PEP 495: Eine Uhrzeit, die es beim Umstellen nicht
  gibt (02:30 Ende März), läuft eine Stunde später nach Wanduhr; eine doppelte Uhrzeit (Ende
  Oktober) läuft einmal, beim ersten Auftreten. Eine geänderte Planung startet mit dem nächsten
  Termin, ein heute schon vergangener feuert nicht nachträglich.
- **Zeitraum:** Mails mit Eingang in `[period_start, period_end)`. `period_end` ist der Termin
  (bzw. „jetzt“ bei manuellen Digests), `period_start` das Ende des letzten nicht
  fehlgeschlagenen Digests (erster Digest: `OLLAMAIL_DIGEST_FIRST_LOOKBACK_HOURS`, höchstens
  `OLLAMAIL_DIGEST_MAX_LOOKBACK_DAYS`). Mails eines fehlgeschlagenen Digests kommen in den nächsten.
- **Inhalt** (`app.digest.content`): Mails aus den gewählten, lesbaren Postfächern, ohne
  Gesendet/Entwürfe/Papierkorb/Spam-Ordner. Reihenfolge nach Triage: Handlungsbedarf, Wichtig,
  Warten auf, eigene/ungetriagte, Info; innerhalb nach Priorität und Eingang. Höchstens
  `OLLAMAIL_DIGEST_MAX_MESSAGES` Mails werden zusammengefasst, der Rest gezählt. Newsletter und
  Benachrichtigungen (`OLLAMAIL_DIGEST_BULK_CATEGORIES`) werden nur in einem Sammelsatz mit
  Anzahl und Absendern erwähnt, Spam (`OLLAMAIL_DIGEST_SKIP_CATEGORIES`) gar nicht. Todos: neue
  offene Todos des Zeitraums sowie überfällige, heute und morgen fällige (Datum in der Zeitzone
  des Nutzers).
- **Zusammenfassung (Map-Reduce, `app.digest.summarize`)**, Aufgabe `digest` des Gateways:
  1. *Map:* Mails in kleinen Gruppen (`OLLAMAIL_DIGEST_MAP_BATCH_SIZE`, begrenzt durch das
     Kontextfenster des zugewiesenen Modells) → je Mail ein Satz plus Termin/Frist
     (strukturierte Ausgabe). Ungültige Antworten: Gruppe wird halbiert und erneut gefragt;
     scheitert eine einzelne Mail, steht stattdessen „Absender schreibt: Betreff“ da.
  2. *Condense:* Passen die Notizen nicht in ein Kontextfenster, werden Gruppen zu weniger
     Notizen zusammengefasst (Referenzen bleiben erhalten), bei Bedarf mehrfach.
  3. *Reduce:* aus den Notizen der gesprochene Hauptteil mit `[n]`-Referenzen. Erfundene
     Referenzen, Überschriften, Listen und `<think>`-Blöcke werden entfernt; eine unbrauchbare
     Antwort wird durch die Notizen ersetzt.
  Zahlen, Datum, Todos und Sammelsätze schreibt der Code selbst (`app.digest.texts`), nicht das
  Modell. Ohne Mails kommt der Digest ohne Modellaufruf aus.
- **Ergebnis `Digest`** (`digests`): Titel, Skript (Markdown, `[n]` verweist auf
  `references` = Mail- und Postfach-IDs), Anzahl Mails/Todos, Modell, Prompt-Versionen,
  Audiodateien je Format, Dauer, Status `pending` → `summarizing` → `synthesizing` → `ready`
  (oder `failed` mit `error_code`). Statuswechsel als Event `digest.changed`.
- **Jobs:** `digest.generate` (Queue `llm`) schreibt das Skript, `digest.synthesize` (Queue `tts`)
  spricht es über `TTSService` (#27) nach `<data_dir>/digests/<user_id>/<digest_id>.mp3|.opus`.
  Beide sind idempotent und per Lock je Digest serialisiert; das Skript wird ohne Titel und
  ohne Referenzen gesprochen. Abgeschaltete Cloud-LLMs und fehlende Stimmen sind dauerhafte Fehler
  (kein Retry).
- **API** (angemeldet, nur eigene Digests, fremde = 404): `GET /api/digests`,
  `POST /api/digests` (jetzt erzeugen, 202; 409 wenn schon einer läuft), `GET/DELETE
  /api/digests/{id}`, `GET /api/digests/{id}/audio.{mp3|opus}` (Range-Requests, Web-Player).
- **Podcast-Feed:** `POST /api/digests/feed` erzeugt ein zufälliges Token (256 Bit) und liefert
  einmalig die URL `/api/feeds/{token}.xml`; gespeichert wird nur der SHA-256-Hash. Erneutes
  `POST` ersetzt, `DELETE /api/digests/feed` widerruft das Token; alte URLs liefern danach 404.
  Der Feed (RSS 2.0 mit iTunes-Namespace, `itunes:block`) enthält fertige Digests mit Audio;
  die Audio-URLs `/api/feeds/{token}/{digest_id}.mp3` sind ebenfalls nur mit Token abrufbar und
  unterstützen `HEAD` und Range-Requests. Feed-Routen sind nicht Teil des OpenAPI-Schemas.
- **Aufbewahrung:** Der stündliche Job `digest.cleanup` löscht Digests älter als
  `OLLAMAIL_DIGEST_RETENTION_DAYS` (Standard 30) samt Dateien, Digests eines inzwischen
  gelöschten Postfachs, Dateien ohne Digest (z. B. gelöschter Nutzer) und markiert seit Stunden
  hängende Digests als fehlgeschlagen.

### 4.5 RAG („Frag deine Inbox“)

- Chunking von Mail-Text und extrahiertem Anhangstext (PDF, DOCX, TXT; später OCR).
- **Hybrid-Retrieval**: Postgres-Volltextsuche (`tsvector`) + pgvector (HNSW), Fusion via Reciprocal Rank Fusion,
  optional Reranker.
- Filter (Zeitraum, Absender, Ordner, Kategorie) werden aus der Frage extrahiert bzw. im UI gesetzt.
- Antworten werden gestreamt (SSE) und enthalten **immer Zitate** mit Links auf die Quell-Mails.
- Strikte Zugriffskontrolle: Retrieval nur über Postfächer, auf die der Nutzer Zugriff hat (Filter in SQL, nicht im Prompt).

**Umsetzung des Index (`backend/app/search/`, #24):**

- **Schritt `index`** (`@registry.step("index", version=1, queue="llm")` in `app.search.tasks`):
  Chunks aus `body_main` (ohne Zitate/Signatur; leer → `body_text`) und aus Anhangstexten,
  je Mail höchstens `OLLAMAIL_SEARCH_MAX_CHUNKS_PER_MESSAGE`. Jeder Chunk hat Kopfdaten als
  Kontext (`From`, `Date`, `Subject`, ggf. `Attachment`), die mit eingebettet und (Gewicht B)
  volltextindiziert werden. Grenzen folgen Absätzen, Zeilen, Sätzen, Wörtern; benachbarte Chunks
  überlappen (`OLLAMAIL_SEARCH_CHUNK_SIZE`/`_OVERLAP`). Jede Mail hat mindestens einen Chunk,
  damit Absender und Betreff immer auffindbar sind. Der Schritt ersetzt die Chunks einer Mail
  (idempotent).
- **Anhänge** (`app.search.extract`): PDF (`pypdf`), DOCX (Standardbibliothek: ZIP + XML, DTDs
  abgelehnt), TXT, HTML. Je Datei ein Kindprozess (`python -m app.search._extract_child`) mit
  leerer Umgebung (keine Secrets), `RLIMIT_AS`/`RLIMIT_CPU`, ohne Dateischreibrechte, nach
  `OLLAMAIL_SEARCH_EXTRACTION_TIMEOUT` beendet. Ergebnisse als Statuscodes (`ok`, `too_large`,
  `timeout`, `unreadable`, `encrypted`, `unsupported`, `missing`). Kein OCR.
- **Tabellen:** `search_chunks` (Text, `ts_config` `german`/`english`/`simple` aus der erkannten
  Sprache, generierte `tsvector`-Spalte mit GIN-Index), `search_embeddings` (`chunk_id`, `model`,
  `embedding vector(n)` mit HNSW-Index, Kosinus), `search_index_state` (aktives Modell). `n` kommt
  aus `OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS`; zur Laufzeit gilt die Länge der Datenbankspalte.
- **Embeddings** über `LLMGateway.embed` (Aufgabe `embeddings`) in Batches
  (`OLLAMAIL_SEARCH_EMBED_BATCH_SIZE`, optional Pause), auf der Queue `llm` mit deren
  Parallelität. Schlägt das Einbetten fehl, werden die Chunks ohne Vektor gespeichert; der Job
  `search.fill_embeddings` ergänzt sie.
- **Modellwechsel:** Vektoren tragen ihr Modell. Weicht das konfigurierte vom aktiven Modell ab,
  rechnet `search.fill_embeddings` alle Chunks batchweise neu; Anfragen nutzen bis zum Abschluss
  das alte Modell (`LLMGateway.embed(model=...)`), dann wird umgeschaltet und aufgeräumt.
  Dimensionswechsel: `python -m app.cli search resize` (`docs/OPERATIONS.md` 3.7).
- **Suche:** `search(session, user_id, query, filters, embedder=..., settings=...)` in
  `app.search.service` liefert Chunks (für #25) oder mit `per_message=True` die beste Stelle je
  Mail (klassische Suche). Volltext: `websearch_to_tsquery` in allen drei Konfigurationen,
  ODER-verknüpft, Rang `ts_rank_cd`. Vektor: Kosinus-Distanz über den HNSW-Index. Je Index
  `OLLAMAIL_SEARCH_CANDIDATES` Kandidaten, Fusion per Reciprocal Rank Fusion
  (`Σ 1/(k + rang)`, `OLLAMAIL_SEARCH_RRF_K`). Ist kein Embedding möglich, nur Volltext.
  Filter: Postfächer, Ordner, Absender, Zeitraum, Quelle (Mail/Anhang).
- **Zugriff:** Jede Abfrage enthält `mailbox_id IN (readable_mailbox_ids(user_id))` aus
  `app.search.access`, der einzigen Stelle dieser Regel. Heute: eigene Postfächer; Shared
  Mailboxes werden dort mit #34 ergänzt.
- **Kategorie-Filter** (`SearchFilters.category_ids`): Triage-Kategorie der Mail
  (`triage_results.category_id`).
- **API der klassischen Suche** (`app.search.router`, #26): `POST /search` mit `query`,
  `filters` (Postfächer, Kategorien, Absender, Zeitraum) und `limit` liefert je Mail den besten
  Treffer (`per_message=True`) mit Betreff, Absender, Datum und einem Ausschnitt um den ersten
  Suchbegriff. POST, damit die Suchanfrage nicht in URLs und Access-Logs landet.

**Umsetzung „Frag deine Inbox“ (`backend/app/rag/`, #25):**

- **Ablauf** (`RagService.ask`, ein Event-Stream pro Frage):
  1. Kontext aus der DB: frühere Runden des Gesprächs, lesbare Postfächer, sichtbare Kategorien.
  2. **Query-Analyse** (Prompt `rag_query@1`, Structured Output `QueryAnalysis`): eigenständige
     Suchanfrage (löst Bezüge in Folgefragen auf) und Filter Zeitraum, Absender, Postfach,
     Kategorie. Das Modell sieht nur die Fragen des Nutzers und die Schlüssel seiner Postfächer
     (`m1`, `m2`, …) und Kategorien, nie Mailinhalte. Übernommen wird nur, was sich auf ein
     lesbares Postfach, eine sichtbare Kategorie oder einen gültigen Zeitraum (Tage in der
     Zeitzone des Nutzers) abbilden lässt. **UI-Filter haben Vorrang**, extrahierte Filter
     füllen nur Lücken (`extracted` nennt sie). Schlägt die Analyse fehl, wird die Frage
     unverändert gesucht. Abschaltbar: `OLLAMAIL_RAG_FILTER_EXTRACTION_ENABLED`.
  3. **Retrieval** über `search()` (Zugriff in SQL, siehe oben), `OLLAMAIL_RAG_RETRIEVAL_LIMIT`
     Chunks. **Reranker** (optional, `app.rag.rerank`): das Chat-Modell ordnet
     `OLLAMAIL_RAG_RERANK_CANDIDATES` Kandidaten (Prompt `rag_rerank@1`); standardmäßig an für
     `gpu-consumer`/`gpu-server`, **aus beim Profil `cpu`**, per
     `OLLAMAIL_RAG_RERANKER_ENABLED` überschreibbar. Fehler → Reihenfolge der Suche.
  4. **Antwort** (Prompt `rag_answer@1`, `LLMGateway.stream`, Aufgabe `rag_chat`): Die besten
     Chunks, die neben Prompt, Verlauf und Antwort (`OLLAMAIL_RAG_MAX_ANSWER_TOKENS`) ins
     Kontextfenster passen, werden nummerierte Quellen. Ohne Treffer wird kein Modell gefragt;
     die Antwort sagt, dass nichts gefunden wurde.
  5. Frage, Antwort und zitierte Ausschnitte speichern; Log-Event `rag_answer_finished` mit
     `ttft_ms` (Anfrage bis erstes Antwortstück), `retrieval_ms`, `total_ms`, Anzahl Quellen
     und Zitate, Status – ohne Inhalte.
- **Zitate:** Das Modell zitiert mit `[n]`. `CitationFilter` liegt zwischen Modell und Client
  und lässt nur Nummern durch, die einer übergebenen Quelle entsprechen (`[1, 3]` → `[1][3]`,
  erfundene Nummern werden entfernt) – auch über Stream-Stücke hinweg. Zitiert die Antwort
  nichts, ist ihr Status `no_evidence` (sonst `answered`); das UI kennzeichnet sie.
- **Prompt-Injection:** Mailinhalte stehen nur in Datenblöcken mit pro Anfrage zufälligem
  Tag (`<mail-3f9a… n="1">…</mail-3f9a…>`), eine Mail kann ihren Block also nicht schließen.
  Der System-Prompt erklärt die Blöcke zu nicht vertrauenswürdigen Daten und verbietet, darin
  enthaltenen Anweisungen zu folgen. Es gibt keine Tools: Die Ausgabe wird nur als Text mit
  Zitatmarkern behandelt, nie ausgeführt. Zugriffskontrolle steht nie im Prompt.
- **Verbindungen:** Für jeden Schritt wird eine eigene DB-Session geöffnet und wieder
  geschlossen; während das Modell schreibt, hält der Stream keine Verbindung.
- **Gesprächsverlauf:** `rag_conversations` (am Nutzer, `ON DELETE CASCADE`), `rag_messages`
  (Frage/Antwort mit `position`, Filtern, Status, Modell, Prompt-Version), `rag_citations`
  (Nummer, Mail, Postfach, Anhang, Kopfzeile, Ausschnitt; `ON DELETE CASCADE` an Antwort, Mail,
  Anhang und Postfach). Folgefragen bekommen die letzten `OLLAMAIL_RAG_HISTORY_TURNS` Runden,
  frühere Antworten ohne Zitatmarker. Beim Lesen eines Gesprächs werden Zitate aus nicht
  (mehr) lesbaren Postfächern per SQL ausgeblendet. Der Job `rag.purge_conversations`
  (täglich) löscht Gespräche nach `OLLAMAIL_RAG_HISTORY_RETENTION_DAYS` ohne neue Frage
  (Standard 90, 0 = nie).

| Endpunkt | Zweck |
|---|---|
| `POST /rag/ask` | Frage stellen (`question`, optional `conversation_id`, `filters`). Antwort als `text/event-stream`: `start` (IDs), `filters` (angewandte Filter), `sources` (nummerierte Quellen mit Mail-ID und Ausschnitt), `token`…, dann `done` (`status`, `citations`, `ttft_ms`) oder `error` (`code`). POST, damit die Frage nicht in URLs/Access-Logs landet; Clients lesen den Stream per `fetch` |
| `GET /rag/conversations` | Eigene Gespräche, zuletzt genutzte zuerst |
| `GET/DELETE /rag/conversations/{id}` | Gespräch mit Fragen, Antworten und Zitaten; löschen. Fremde Gespräche verhalten sich wie nicht vorhandene (404) |
| `DELETE /rag/conversations` | Alle eigenen Gespräche löschen |

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

### Umsetzung (`backend/app/auth/`, `backend/app/users/`)

**Datenmodell:** `users` (E-Mail normalisiert und eindeutig, Anzeigename, Rolle `admin|user`,
Sprache, Zeitzone, aktiv), `auth_identities` (`provider`, `subject`, `user_id`; ein Nutzer kann
mehrere Identitäten haben; lokal: `provider=local`, `subject` = Nutzer-ID, Argon2id-Hash),
`auth_sessions`, `auth_rate_limits`. Alles hängt per `ON DELETE CASCADE` am Nutzer.

**Provider-Interface** (`app/auth/providers/base.py`): Ein Provider beweist nur, wer jemand ist,
und liefert eine `VerifiedIdentity(provider, subject, email, display_name, groups,
email_verified)`. `PasswordAuthProvider.authenticate(login, password)` für lokale Konten und
LDAP (#32), `RedirectAuthProvider.authorization_url(...)`/`complete(...)` (mit `state`, `nonce`
und PKCE-`code_verifier`) für OIDC (#30) und GitHub (#31). Sperre, Session und Rollenprüfung
sind für alle Provider gleich. Externe Provider stehen in `app.state.auth_providers`: fest per
`register` oder als *Quelle* per `add_source` (z. B. OIDC-Provider aus der Datenbank, pro Anfrage
gelesen, damit Änderungen sofort auf allen API-Instanzen gelten). `GET /api/auth/providers`
listet sie für die Login-Seite (Redirect-Provider mit `login_path`), zusätzlich die aktiven
LDAP-Verzeichnisse aus der Datenbank.

**JIT-Provisioning** (`app/auth/provisioning.py`, für alle externen Provider):
`provision_user(db, identity, policy, role=...)` meldet bekannte Identitäten an (Gruppen werden in
`auth_identities.groups` aktualisiert, sofern der Provider sie speichert) oder legt den Nutzer beim
ersten Login aus E-Mail-Adresse und Anzeigename an. Ein vorhandenes Konto mit derselben Adresse
wird nur verknüpft, wenn der Provider es erlaubt (`link_by_email`) **und** die Adresse als
verifiziert meldet; sonst 409 (`account-exists`), denn wer ein E-Mail-Attribut im externen
Verzeichnis setzen darf, könnte sonst ein lokales (Admin-)Konto übernehmen. Dazu kommen
Domain-Allowlist und Abschalten der Kontoanlage je Provider. `role` kommt aus dem Gruppen-Mapping
des Providers; `None` heißt, der Provider verwaltet keine Rollen. Kontoanlage und Rollenwechsel
landen im Audit-Log. Fehler sind `ProvisioningError` (ein `ProblemError` mit statischem `code`).

**Externe Logins im Browser** (`app/auth/redirect_flow.py`): Der Flow für Redirect-Provider
(verschlüsseltes Einmal-Cookie mit `state`, `nonce`, PKCE-Verifier; Fehler als Redirect auf
`/login?error=<code>`) ist providerunabhängig; GitHub (#31) nutzt ihn mit.

**OIDC** (`app/auth/providers/oidc/`, Anleitung: [`auth/oidc.md`](auth/oidc.md)): Provider aus der
Datenbank (`auth_oidc_providers`, Client-Secret als `EncryptedStr`, Admin-API unter
`/api/admin/auth/oidc`) und aus `OLLAMAIL_AUTH_OIDC_PROVIDERS` (read-only). Discovery und JWKS
werden je Issuer gecacht; ID-Token-Prüfung mit `joserfc`, PKCE-/Client-Auth-Helfer aus Authlib.
Presets für Entra ID (`tid`-Prüfung, Multi-Tenant nur mit Tenant-Allowlist), Google Workspace
(`hd`), Keycloak, Authentik und generisch. `POST /api/auth/oidc/logout` liefert zusätzlich die
URL für das RP-initiated Logout.

**GitHub** (`app/auth/providers/github/`, Anleitung: [`auth/github.md`](auth/github.md)): OAuth App
oder GitHub App, github.com oder GitHub Enterprise Server (`base_url`, API unter `/api/v3`).
Provider stehen in `auth_github_providers` (Client-Secret als `EncryptedStr`, Admin-API unter
`/api/admin/auth/github`) und nutzen Redirect-Flow und Provisioning von OIDC. Subject ist die
numerische GitHub-Nutzer-ID; E-Mail nur die verifizierte primäre Adresse. Org- und
Team-Beschränkung (`allowed_organizations`, `allowed_teams`) wird serverseitig über die REST-API
geprüft; Teams (`<org>/<team-slug>`) sind die Gruppen für das Rollen-Mapping (#33).

**LDAP / Active Directory** (`app/auth/providers/ldap/`, Details: [`auth/ldap.md`](auth/ldap.md)):
Verzeichnisse stehen in `auth_ldap_directories` (Einstellungen als JSONB, Bind-Passwort
verschlüsselt) und werden über `/api/auth/ldap/directories` (nur Admins) gepflegt und getestet.
Login über `POST /api/auth/login/ldap/{name}` mit denselben Rate-Limits wie der lokale Login.

**Bootstrap:** `GET /api/setup/status` → `{"initialized": bool}`. `POST /api/setup` legt den ersten
Admin an und meldet ihn an. Voraussetzung ist der Setup-Token (`OLLAMAIL_SETUP_TOKEN` oder per
HKDF aus `OLLAMAIL_SECRET_KEY` abgeleitet, auf allen API-Instanzen gleich, beim Start geloggt
und per `python -m app.cli setup-token` abrufbar). Ein transaktionaler Advisory Lock
(`pg_advisory_xact_lock`) serialisiert parallele Requests: genau einer gewinnt, alle anderen und
jeder spätere Versuch erhalten 409. Danach ist die Selbstregistrierung aus
(`OLLAMAIL_AUTH_LOCAL_REGISTRATION`); Admins legen Konten über `POST /api/users` an.
Notfallzugang: `python -m app.cli create-admin`.

**Login** (`POST /api/auth/login`): Zuerst zählen zwei Fixed-Window-Zähler in Postgres
(atomares Upsert, vor der Passwortprüfung committet): pro Client-IP (`OLLAMAIL_AUTH_IP_MAX_ATTEMPTS`)
und pro Konto (`OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS` je `OLLAMAIL_AUTH_LOGIN_WINDOW_MINUTES`). Darüber
gibt es 429 (`retry_after` in Sekunden), auch bei richtigem Passwort; ein erfolgreicher Login setzt
den Kontozähler zurück. Unbekannte Konten werden genauso gezählt und mit einem Dummy-Hash geprüft,
damit Antwort und Laufzeit nichts verraten. Die Schlüssel sind HMACs von IP bzw. E-Mail-Adresse.
Argon2id (RFC 9106, 64 MiB) läuft in einem Thread, höchstens vier Hashes gleichzeitig; veraltete
Parameter werden beim Login aktualisiert. Hinter einem Reverse Proxy kommt die Client-IP aus
`X-Forwarded-For` (uvicorn `--forwarded-allow-ips`).

**Sessions:** Cookie `ollamail_session` (`HttpOnly`, `Secure`, `SameSite=Lax`, 256 Bit Zufall);
in der DB steht nur der SHA-256. Gültig bis `expires_at` (Lebensdauer) und solange die letzte
Anfrage weniger als das Idle-Timeout zurückliegt (`last_seen_at`, höchstens minütlich
geschrieben). Jede Anfrage prüft Rolle und `is_active` neu; deaktivierte Nutzer verlieren sofort
den Zugriff. Login ersetzt eine vorhandene Session (keine Session Fixation). Endpunkte:
`GET /api/auth/me`, `PATCH /api/auth/me` (Name, Sprache, Zeitzone), `POST /api/auth/logout`,
`GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`, `DELETE /api/auth/sessions`
(alle anderen; mit `?include_current=true` alle). Der Worker-Job `auth.cleanup` löscht stündlich
abgelaufene Sessions und Zähler.

**CSRF:** Signiertes Double-Submit-Cookie (`CSRFMiddleware`, gilt für die ganze App). Jede
Anfrage außer `GET`/`HEAD`/`OPTIONS`/`TRACE` muss den Wert des Cookies `ollamail_csrf` im Header
`X-CSRF-Token` senden. Der Token ist `<nonce>.<HMAC(nonce, Session-Cookie)>`: an die Session
gebunden, bei Login/Logout neu ausgestellt und von einer Subdomain aus nicht fälschbar. Fehlt das
Cookie oder passt es nicht zur Session, setzt jede Antwort ein neues. `Sec-Fetch-Site: cross-site`
wird zusätzlich abgewiesen.

**Dependencies:** `get_current_session` (401), `require_admin` (403), `get_current_user` (ORM-Objekt)
in `app/auth/dependencies.py`; `get_current_user_id` in `app/core/current_user.py`. Die DB-Session
der Auth-Prüfung ist nach der Prüfung wieder frei (`Depends(get_db, scope="function")`), damit
SSE-Streams keine Pool-Verbindung halten.

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
- Anmeldung und Route-Guards: Der Root-Route-Guard lädt `GET /api/setup/status` und
  `GET /api/auth/me`. Nicht eingerichtet → `/setup` (Erst-Admin), ohne Session →
  `/login?redirect=…`; jede Route ist geschützt, außer sie ist ausdrücklich öffentlich (`/login`,
  `/setup`). Admin-Seiten zeigen Nicht-Admins eine 403-Seite; durchgesetzt wird es in der API.
  Externe Provider kommen dynamisch aus `GET /api/auth/providers`. Details: `frontend/README.md`.
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
