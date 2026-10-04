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
  notifications/ Benachrichtigungen bei wichtigen Mails: Opt-in je Nutzer, Event nach der Triage, Web Push
  todos/         Extraktion, CRUD; export/: Export nach CalDAV und Microsoft To Do (TodoSink-Interface)
  digest/        Tageszusammenfassung, TTS, Podcast-Feed
  search/        Suchindex: Chunking, Anhangstexte, Embeddings, Hybrid-Suche (RRF)
  rag/           Chat, Zitate (nutzt search/)
  admin/         Instanz-Einstellungen, Auth-Provider, Audit-Log, Statistiken
  audit/         Audit-Log: record(), append-only Tabelle mit Hash-Kette, Admin-API (Liste, CSV)
  privacy/       Aufbewahrungsfristen (Admin + täglicher Job), Datenexport (ZIP), Konto-/Nutzerlöschung
  worker.py      Procrastinate-App und Task-Registrierung
```

## 3. Zentrale Abstraktionen

### 3.1 Mail-Provider

```python
class MailProvider(Protocol):  # app/mail/providers/base.py
    capabilities: ProviderCapabilities  # labels, push, server_threads, keywords
    async def list_folders(self) -> list[RemoteFolder]: ...
    def fetch_since(self, folder_id: str, cursor: SyncCursor | None, *, since: datetime | None = None
                    ) -> AsyncIterator[SyncEvent]: ...  # MessageFetched | MessageUpdated | MessageChanged | MessageDeleted | FlagsReported | CursorAdvanced
    def watch(self, folder_id: str | None = None) -> AsyncIterator[ChangeEvent]: ...  # IMAP IDLE / Graph Webhooks / Gmail Push
    async def move(self, remote_ref: str, target_folder_id: str) -> str: ...  # neue Referenz (IMAP-UIDs ändern sich)
    async def set_flags(self, remote_ref: str, flags: frozenset[str]) -> None: ...
    async def apply_label(self, remote_ref: str, label: str) -> None: ...  # Gmail: Label, Graph: Kategorie, IMAP: Keyword oder Ordner
    async def remove_label(self, remote_ref: str, label: str) -> None: ...
    async def send(self, reply: OutgoingReply) -> SentMessage: ...  # Antwort senden, Kopie in „Gesendet“
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
- **Senden** (#92): `send(OutgoingReply)` bekommt die fertige RFC-5322-Quelle, für alle Provider
  einmal in `app/mail/compose.py` gebaut (Empfänger, `Re:`, `In-Reply-To`, `References`,
  `Message-ID`), plus die strukturierten Felder für Provider, die serverseitig zusammensetzen
  (Graph). Senden wird nie automatisch wiederholt (kein doppelter Versand); abgelehnte Mails
  sind `SendError` mit Code. Der Server legt die gesendete Kopie ab, der nächste Sync bringt sie.
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
mit `ON DELETE CASCADE`) oder ist shared (`is_shared`, per CHECK erzwungen). Shared Mailboxes
werden Nutzern und Gruppen über `mail_mailbox_assignments` zugewiesen (siehe §5, Shared
Mailboxes).

| Provider | Phase | Auth | Sync | Hinweise |
|---|---|---|---|---|
| IMAP/SMTP | MVP | Passwort/App-Passwort, XOAUTH2 vorbereitet | UIDVALIDITY/UID + `IDLE`, CONDSTORE/QRESYNC falls verfügbar | Funktioniert mit jedem Server |
| Microsoft 365 | v1 (#37) | OAuth2 (Entra ID App, delegiert oder App-only mit Admin-Consent) | Graph Delta Query, Polling; Change Notifications optional | Shared Mailboxes über App-Permissions + RBAC for Applications / `ApplicationAccessPolicy` |
| Gmail / Google Workspace | v1 | OAuth2, Workspace: Domain-wide Delegation | `history.list`, Polling (Standard) oder Pub/Sub-Pull (optional) | Labels statt Ordner; Details: [`providers/gmail.md`](providers/gmail.md) |

Postfach-Zugangsdaten und OAuth-Tokens werden verschlüsselt gespeichert (siehe `PRIVACY.md`;
Spalte `mail_mailboxes.credentials` vom Typ `EncryptedJSON` aus `app/core/crypto.py`).

#### Mail-Aktionen auf dem Server

ollamail ist ein Analyse-Werkzeug, kein Mail-Client: keine neuen Mails, keine Ordnerverwaltung,
kein endgültiges Löschen. Aus der App heraus gehen nur diese Änderungen an den Server, jede erst
nach einer ausdrücklichen Aktion des Nutzers:

| Aktion | Wo | Provider-Methode | Recht |
|---|---|---|---|
| Gelesen/ungelesen, Markieren (Flag/Stern) | `PATCH /messages/{id}`, Job `mail.write_flags` | `set_flags` | `act` |
| Archivieren, Verschieben, In den Papierkorb (#148) | `POST /messages/{id}/actions` (`app/mail/actions.py`) | `move` | `act` |
| Antwort senden | `POST /drafts/{id}/send` (§4.6) | `send` | `send` |
| Triage-Kategorie zurückschreiben (opt-in je Postfach) | `app/triage/writeback.py` (§4.2) | `apply_label`/`remove_label` bzw. `move` | `manage` |

**Archivieren, Verschieben, Papierkorb** (#148): Ziel ist ein Ordner des Postfachs, über seine
Rolle gefunden – Archivieren in den Ordner mit Rolle `archive` (Gmail: das Label „All Mail“, also
`INBOX` entfernen), Papierkorb in den mit Rolle `trash`, Verschieben in einen beliebigen Ordner
bzw. ein Label (Gmail: Label hinzu, `INBOX`/`SPAM`/`TRASH` weg). Damit genügt allen Providern
`move`; ohne passenden Ordner antwortet die API 409 `no_archive_folder` bzw. `no_trash_folder`.
Die Aktion läuft synchron im Request (Zeile gesperrt, damit Aktion und Sync sich nicht
überholen): erst auf dem Server, dann folgt die gespeicherte Mail – neue `remote_ref` (IMAP-UIDs
ändern sich) und Ordner. Der nächste Sync bestätigt das, ohne Kopie. Die Antwort nennt
`undo_folder_id` (der Posteingang, wenn die Mail dort lag, sonst ihr bisheriger Ordner);
„Rückgängig“ ist ein `move` dorthin. Fehler des Servers ändern lokal nichts (`409
message_not_found`, `502` mit Code, `503` bei Verbindungsfehlern). Jede Aktion steht im Audit-Log
(`mail.moved`, Markieren `mail.flagged`; nur IDs), Events `message.updated` gehen an alle Leser
des Postfachs.

Bekannte Grenze: Der Papierkorb ist standardmäßig vom Sync ausgeschlossen. Bei IMAP bleibt eine
dorthin verschobene Mail deshalb gespeichert (unter ihrer neuen Referenz), bei Graph und Gmail
entfernt sie der nächste Sync; „Rückgängig“ wirkt dort nur bis zu diesem Sync (danach 404).

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
- **Zielprüfung** (`app/core/network.py`, gilt für IMAP, SMTP und den CalDAV-Export): Der Host
  wird einmal aufgelöst; nur global erreichbare Adressen sind erlaubt. Loopback, RFC 1918,
  Link-Local (inkl. `169.254.169.254`), ULA, CGNAT, Multicast und reservierte Bereiche nur, wenn
  der Hostname oder ein passender Bereich in `OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS` (CalDAV:
  `OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS`) steht. Verbunden wird mit der
  geprüften Adresse (TLS prüft weiter den Hostnamen), damit DNS-Rebinding nicht greift. Ein
  abgelehntes Ziel liefert denselben Fehler wie ein geschlossenes (`connection_failed`); so taugen
  Verbindungstests nicht als Portscanner für interne Dienste (`postgres:5432`, `ollama:11434`).
- **Ordner:** `LIST` (mit `RETURN (SPECIAL-USE)`, falls verfügbar). Rollen aus Special-Use-Attributen,
  sonst aus gängigen Namen (DE/EN, nur oberste Ebene). `remote_id` ist der Ordnername wie vom
  Server (modified UTF-7), `name` dekodiert.
- **Referenzen:** `remote_ref = "<UIDVALIDITY>:<UID>:<Ordner>"`.
- **Cursor** pro Ordner: `uidvalidity`, `high` (höchste gesehene UID), `modseq`
  (HIGHESTMODSEQ), `known` (gespeicherte UIDs als kompaktes Sequence-Set), `import`
  (offener Initialimport: `since`, `below`) und `flags_at` (letzter vollständiger Flag-Abgleich
  ohne CONDSTORE).
- **Ablauf von `fetch_since`:** (1) Änderungen bekannter Mails – mit QRESYNC ein
  `UID FETCH … (CHANGEDSINCE m VANISHED)`, mit CONDSTORE `CHANGEDSINCE` plus UID-Suche für
  Löschungen, sonst (#147) die Flags der neuesten `OLLAMAIL_MAIL_IMAP_FLAG_WINDOW` (Standard 1000)
  bekannten Mails plus eine UID-Suche für Löschungen und Verschiebungen; alle Flags nur alle
  `OLLAMAIL_MAIL_IMAP_FULL_FLAG_SCAN_HOURS` (Standard 24, Zeitpunkt im Cursor als `flags_at`).
  Die Flags gehen gebündelt als `FlagsReported` (je 10 000) an die Engine, die sie in einer
  temporären Tabelle mit dem Bestand vergleicht und nur Abweichungen schreibt – statt eines
  Events und einer Abfrage je Mail. Älteres als das Fenster, das in einem anderen Client
  gelesen/markiert wurde, erscheint so spätestens nach einem Tag; (2) neue Mails (UID > `high`);
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
- **Senden** (`smtp.py`): SMTP-Submission mit `smtplib` im Thread, eine Verbindung pro Mail.
  Einstellungen `smtp_host` (Standard: IMAP-Host), `smtp_port` (Standard 587 bzw. 465),
  `smtp_security` (`starttls` Standard, `tls`, `none` nur mit Admin-Flag), `smtp_username`
  (Standard: IMAP-Login), `smtp_save_sent`; Passwort `credentials.smtp_password`, sonst das
  IMAP-Passwort, bei `xoauth2` das Access-Token (`AUTH XOAUTH2`). Zertifikatsprüfung wie bei IMAP.
  Danach `APPEND` mit `\Seen` in den Ordner mit Rolle „Gesendet“ (fehlt er, wird `Sent`
  angelegt); scheitert nur die Kopie, gilt die Mail als gesendet (`sent_copy_error`).

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
- **Senden:** `createReply` bzw. `createReplyAll` auf der Original-Mail (Empfänger, Betreff,
  Text des Entwurfs), dann `send`; Exchange setzt die Threading-Header und legt die Kopie in
  „Gesendete Elemente“ ab. Delegiert mit einem eigenen Token mit `Mail.Send`
  (`Mail.Send.Shared` für fremde Postfächer), das beim Verbinden angefordert wird
  (`OLLAMAIL_MAIL_GRAPH_SEND_ENABLED`); der Sync behält seine bisherigen Scopes. App-only braucht
  die Anwendungsberechtigung `Mail.Send`. Ohne Berechtigung: `send_not_permitted`.

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
- **Senden:** `messages.send` mit der RFC-5322-Quelle und der `threadId` der Original-Mail;
  Gmail legt die Kopie unter SENT ab. `gmail.modify` deckt das Senden ab, mit
  `OLLAMAIL_GMAIL_READONLY` wird es abgelehnt (`read_only`).

#### Sync (`backend/app/mail/sync/`)

- **`engine.sync_mailbox`** (providerunabhängig): Ordnerliste spiegeln (neue Ordner, auf dem
  Server gelöschte Ordner samt Mails löschen; Ordner mit ausgeschlossener Rolle aus
  `SyncSettings.excluded_roles` – Standard Papierkorb und Spam – werden angelegt, aber nicht
  synchronisiert), dann je Ordner (INBOX zuerst) `fetch_since` anwenden: `MessageFetched` →
  `store_message`, `MessageUpdated` → Flags/Ordner, `MessageDeleted` → `delete_messages`,
  `FlagsReported` → Flags vieler Mails im Block über eine temporäre Tabelle abgleichen,
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
- **Zeitscheiben (#141):** Ein Lauf importiert höchstens `OLLAMAIL_MAIL_SYNC_SLICE_BATCHES`
  Batches bzw. `OLLAMAIL_MAIL_SYNC_SLICE_MINUTES` Minuten Altbestand (Events mit `initial=True`).
  Danach endet er am zuletzt committeten Cursor, die übrigen Ordner bekommen nur noch Änderungen
  und neue Mails, und `SyncStats.incomplete` ist gesetzt; der Job reiht einen Folgelauf mit
  niedrigerer Priorität (`CONTINUE_PRIORITY`) ein. Weil die Provider neue Mails vor dem
  Weiterimport liefern, warten neue Mails höchstens eine Zeitscheibe. Ein Resync ohne
  Zwischen-Cursor (`reconcile`) wird nicht geteilt.
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
und den Lösch-Job (`app/mail/deletion.py`); sie baut nichts davon nach.

| Endpunkt | Zweck |
|---|---|
| `POST /mailboxes/autodiscover` | Host/Port-Vorschläge zur Adresse (bekannte Anbieter, sonst `imap.<domain>`/`mail.<domain>`). Offline, keine DNS-/HTTP-Abfragen; Adresse im Body, damit sie nicht in Access-Logs landet. Hinweise als Codes (`app_password`, `enable_imap`, `oauth_required`, …) |
| `POST /mailboxes/test` | Verbindungstest ohne Speichern (Provider aus der Registry, `list_folders`). Ergebnis `ok`, Fehlercode oder Ordnerliste für die Ordnerauswahl vor dem Anlegen. Rate-Limit pro Nutzer (`OLLAMAIL_MAIL_CONNECTION_TEST_MAX_ATTEMPTS` je 10 Minuten, gemeinsam mit Anlegen und Verbindungsänderung; darüber 429) |
| `GET/POST /mailboxes` | Eigene Postfächer mit Sync-Status; Anlegen testet die Verbindung (422 mit `error_code`), lehnt Duplikate ab (409) und stößt den Initialimport an |
| `GET/PATCH/DELETE /mailboxes/{id}` | Details; Umbenennen, Verbindung/Zugangsdaten (vor dem Speichern getestet), Importzeitraum, ausgeschlossene Rollen, Pausieren/Fortsetzen (`sync_enabled`); Entfernen (202, im Hintergrund, siehe unten) |
| `GET /mailboxes/{id}/status` | Nur der Sync-Status |
| `POST /mailboxes/{id}/sync` | Sync sofort anstoßen (202, `queued`); 409, wenn pausiert |
| `GET/PATCH /mailboxes/{id}/folders` | Ordner mit Auswahl und Status je Ordner; Auswahl setzen |
| `GET /mailboxes/providers` | Postfachtypen, die sich auf dieser Instanz anlegen lassen: `credentials` (Formular, z. B. IMAP) oder `oauth` mit `oauth_start_path`. OAuth-Typen erscheinen nur, wenn der Provider registriert und sein OAuth-Client konfiguriert ist (`app/mail/api/providers.py`, ein Eintrag je Provider) |

- **Zugriff:** ausschließlich über `app/mail/access.py` (`accessible_mailbox_ids`, `visible_to`,
  `get_mailbox`, Berechtigungen `read`/`sync`/`manage`/`act`/`send`). Besitzer haben alle, Nutzer
  eines Shared Mailbox `read` und mit einer `act`-Zuweisung zusätzlich `act`, nie `send`. Fremde Postfächer verhalten sich wie nicht vorhandene (404).
  `MailboxRead.permissions` nennt die Berechtigungen des angemeldeten Nutzers;
  `provider_settings` sehen nur Nutzer mit `manage`. `GET /mailboxes/{id}/members` listet alle,
  die das Postfach lesen dürfen (für die Zuweisung von Team-Todos).
- **Zugangsdaten** sind write-only (Antworten enthalten nur `has_credentials`) und werden
  verschlüsselt gespeichert. Ein PATCH ersetzt sie als Ganzes; neue Verbindungsdaten werden mit den
  gespeicherten Zugangsdaten getestet.
- **Sync-Status** (`MailboxSyncStatus`): `phase` = `paused` | `error` (letzter Sync für das ganze
  Postfach fehlgeschlagen) | `syncing` (Sync-Job wartet oder läuft, aus `procrastinate_jobs`) |
  `pending` (nie synchronisiert) | `importing` (Initialimport eines Ordners offen) | `idle`, dazu
  letzte Synchronisierung, Fehlercode, Ordner gesamt/importiert/fehlgeschlagen und Anzahl Mails.
  Die Anzahl wird für Postfächer in `syncing`/`importing`/`pending` je API-Prozess bis zu 15 s
  wiederverwendet (#188, `service.message_counts`): Während eines Imports lädt jeder Tab den Status
  nach jedem Batch neu, und `count(*)` über 100k Mails ist dafür zu teuer. Ruhende Postfächer
  werden bei jedem Request gezählt.
  „Import offen“ heißt: kein Cursor oder der Cursor enthält den Schlüssel `import`
  (Konvention für Provider, die in Batches importieren, siehe `SyncCursor`).
- **Ordnerauswahl** setzt `Folder.sync_enabled` und hält `SyncSettings.excluded_folders`
  synchron (bleibt erhalten, wenn ein Ordner neu angelegt wird). Abgewählte Ordner behalten ihre
  gespeicherten Mails. Ordner mit ausgeschlossener Rolle (Papierkorb, Spam) bleiben aus, bis die
  Rolle aus `excluded_roles` entfernt wird. Der Importzeitraum gilt für Ordner, deren Import noch
  nicht begonnen hat.
- **Entfernen im Hintergrund** (#147): Ein Postfach kann 200k Mails mit Anhängen, Chunks und
  Embeddings haben; eine Kaskade in einem Statement hielt den Request minutenlang offen.
  `DELETE` setzt deshalb nur `deletion_requested_at`, pausiert den Sync, schreibt
  `mailbox.deleted` ins Audit-Log und reiht den Job `mail.delete_mailbox` ein (Queue `default`,
  eigener Lock `mailbox_deletion:<id>`, damit ein laufender Import ihn nicht aufhält; ein noch
  laufender Sync scheitert spätestens am fehlenden Postfach). `access.accessible_mailbox_ids`
  schließt markierte Postfächer aus, also verschwinden ihre Daten sofort überall; nur
  `GET /mailboxes` (`access.listed_to`) und die Admin-Liste zeigen sie mit `status.phase =
  deleting`, alle anderen Endpunkte antworten 404. Der Job löscht Mails in Batches zu 500,
  neueste zuerst, per Keyset über den Index `(mailbox_id, sort_date, id)` (ohne Keyset müsste jeder
  Batch die Indexeinträge der schon gelöschten Zeilen überspringen: 42 s statt 6 s für 100k
  Mails), dann Threads, dann die Postfachzeile mit dem Rest und das Anhangsverzeichnis.
  `mail.resume_deletions` (alle 15 Minuten) reiht verlorene Löschungen erneut ein. Dasselbe
  Postfach kann sofort wieder hinzugefügt werden; die Duplikatprüfung ignoriert markierte.
  Nach dem Löschen der Postfachzeile ruft der Job die Handler von
  `app.mail.hooks.on_mailbox_deleted` mit dem früheren Besitzer auf. Darüber wartet das Löschen
  eines Nutzers (#177, `docs/PRIVACY.md`) auf seine Postfächer: Der Request markiert Nutzer und
  Postfächer mit demselben Mechanismus, `privacy.delete_user` löscht die Nutzerzeile, sobald
  kein Postfach mehr übrig ist.
- **Events:** Neben `mailbox.sync` aus dem Sync sendet die API `mailbox.changed`
  (`created`, `updated`, `deleting`) an den Besitzer bzw. die Leser, der Lösch-Job am Ende
  `deleted`.
- **Jobs aus der API:** `app/core/jobs.py` öffnet die Procrastinate-App beim ersten Einreihen
  (der Start der API hängt nicht an der Queue) und schließt sie beim Shutdown.
- **Audit:** `mailbox.created` und `mailbox.deleted` (beim Anfordern der Löschung) mit dem Nutzer
  als Akteur, in derselben Transaktion wie die Änderung.

#### Mail-Lese-API (`backend/app/mail/api/messages.py`)

Grundlage der Inbox (#16). Zugriff wie bei der Postfach-API über `access.visible_to`: Mails fremder
Postfächer antworten 404.

**Listen bei großen Postfächern** (#140, `app/mail/listing.py`): Sortierschlüssel ist die Spalte
`mail_messages.sort_date` (`received_at`, sonst `sent_at`, sonst `created_at`; `NOT NULL`, gepflegt
vom Trigger `mail_messages_sort_date`) mit dem Index `(mailbox_id, sort_date DESC, id DESC)`. Eine
Seite liest den Index in Listenreihenfolge und hört nach `limit` Zeilen auf, egal wie tief sie in
der Liste liegt; bei mehreren Postfächern wird jedes einzeln gelesen und zusammengeführt (ein
`mailbox_id IN (…)` müsste alle Mails sortieren). Ordnerfilter laufen über Ordner-IDs statt über einen
Join auf `mail_folders`: Die kleine Tabelle hat oft keine Statistik, und eine Fehlschätzung lässt den
Planer sonst alle Mails sortieren. `unread=true` liest den partiellen Index
`ix_mail_messages_unread` (gleiche Spalten, `WHERE NOT (flags @> ARRAY['seen']::text[])`, #186):
Bei wenigen Ungelesenen in einem großen Postfach liest eine Seite nur die Ungelesenen statt alle
gelesenen zu überspringen. Der Filter muss genau dieses Prädikat mit dem Array als Literal
verwenden (`listing.UNREAD`), sonst kann der Planer den Index nicht nehmen. Gezählt wird nur für
die erste Seite, und zwar ab der Ordnerzuordnung (`mail_message_folders`, Index-only über den
Ordner): Die Zählung kostet die Größe des Ordners, nicht die des Postfachs; nur mit `unread` wird
`mail_messages` gelesen. Listenzeilen laden statt `body_main` nur dessen Anfang für das Snippet
(`left(body_main, 200)`, `Message.snippet`). Nachweis mit 100k synthetischen Mails:
`backend/tests/perf/` (`OLLAMAIL_TEST_PERF=1`, in der CI aktiv) prüft per `EXPLAIN (ANALYZE)`, dass
jede Seite ihre Zeilen aus einem Listenindex liest und höchstens wenige Seiten Zeilen anfasst –
auch für ein archivlastiges Postfach mit wenigen Ungelesenen und kleinen Triage-Segmenten.

| Endpunkt | Zweck |
|---|---|
| `GET /messages` | Eine Zeile je Mail, neueste zuerst, Keyset-Paging (`cursor`, `limit` ≤ 200), `total` für die virtualisierte Liste (nur auf der ersten Seite, danach `null`). Filter: `mailbox_id`, `folder_id` (ohne: Ordner mit Rolle `inbox`), `unread`. Ohne Bodies; nur ein Snippet aus `body_main` |
| `GET /messages/{id}/thread` | Konversation der Mail, älteste zuerst (höchstens die neuesten 100), mit Empfängern, Snippet und Anhängen. Den Body (`body`: sanitisiertes HTML `html`, `blocked_images`, Text `text`) haben nur die geöffnete und die neueste Mail – die, die das UI aufgeklappt zeigt; alle anderen `body: null` (#210) |
| `GET /messages/{id}/body` | Body einer Mail (`html`, `blocked_images`, `text`): beim Aufklappen einer eingeklappten Mail im Thread. Mit `?external_images=true` HTML mit externen Bildern – erst, wenn der Nutzer sie für diese Mail anfordert |
| `PATCH /messages/{id}` | `{"seen": bool, "flagged": bool}` (beide optional): gelesen/ungelesen, markieren. Sofort gespeichert, Event `message.updated` an alle Leser, Job `mail.write_flags` schreibt die Flags auf den Server; Markieren steht im Audit-Log (`mail.flagged`). Braucht `act`, sonst 403 `read_only` (der Status gilt für das ganze Postfach) |
| `POST /messages/{id}/actions` | `{"action": "archive" \| "trash" \| "move", "folder_id"}` (#148, `app/mail/api/actions.py`): synchron auf dem Server, Antwort mit neuen `folder_ids` und `undo_folder_id`. Braucht `act` (403 `read_only`); 409 `no_archive_folder`/`no_trash_folder`/`message_not_found`, 422 `unknown_folder`, 502/503 bei Serverfehlern. Siehe §3.1, „Mail-Aktionen auf dem Server“ |
| `GET /messages/{id}/attachments/{attachment_id}` | Download (`Content-Disposition: attachment`, `application/octet-stream`, `nosniff`, CSP `sandbox`). `?inline=true` nur für PNG/JPEG/GIF/WebP (`cid:`-Bilder im HTML) |

- **HTML:** immer serverseitig mit `sanitize_html` bereinigt; das Roh-HTML verlässt den Server nie.
  Das Bereinigen läuft per `asyncio.to_thread` außerhalb des Event-Loops, damit es andere Requests
  nicht blockiert (#147); ebenso `normalize_message` (MIME-Parsing) beim Speichern im Worker.
  Der Thread lädt und bereinigt höchstens zwei Bodies (geöffnete und neueste Mail), nicht alle
  bis zu 100 (#210); das Frontend lädt weitere beim Aufklappen über `/body` nach (TanStack Query,
  Skeleton während des Ladens).
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
  metrics.py        LLMCallMetrics + MetricsSink (Standard: Log-Event `llm_call` und Prometheus-Zähler)
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
4. **Antwortlimit und Frist (#132):** Jeder Aufruf hat ein Limit für erzeugte Tokens (Ollama
   `num_predict`, OpenAI-kompatibel `max_tokens`). Setzt das Feature keins, gilt
   `OLLAMAIL_LLM_TASK_<TASK>_MAX_TOKENS` → `OLLAMAIL_LLM_MAX_OUTPUT_TOKENS` (falls gesetzt) →
   eingebauter Task-Default (1024, `todos` 800). Feste Werte setzen u. a. Triage (200),
   RAG-Query-Analyse (256) und Reranking (32 + 4 je Kandidat). Abgeschnittenes JSON läuft in die
   Retry-/`LLMOutputError`-Behandlung (Punkt 5). Zusätzlich hat jeder Generierungsaufruf eine
   **Gesamtfrist** (`OLLAMAIL_LLM_TASK_<TASK>_CALL_TIMEOUT` → `OLLAMAIL_LLM_CALL_TIMEOUT` → Profil:
   `cpu` 180 s, GPU-Profile 60 s; Digest, RAG-Chat und Antwortentwürfe das Doppelte). Sie gilt ab
   dem LLM-Slot, umfasst alle Structured-Output-Versuche und bei Streams die ganze Antwort, nicht
   nur die Pause zwischen zwei Bytes wie der HTTP-Timeout (`OLLAMAIL_LLM_TIMEOUT`). Bei Streams
   wird nur das Warten auf den nächsten Chunk abgebrochen, nie Code des Aufrufers. Ablauf der Frist
   und HTTP-Lese-Timeouts ergeben `LLMTimeoutError` (Unterklasse von `LLMUnavailableError`), in
   Metriken und Logs als eigener `error_type` sichtbar; ein Verbindungs-Timeout bleibt
   `LLMUnavailableError` („nicht erreichbar“). Embeddings haben keine Gesamtfrist.
5. **Structured Output:** Das JSON-Schema geht nativ an den Server (Ollama `format`, OpenAI
   `response_format`) und zusätzlich in den System-Prompt. Ungültige Antworten werden mit einem
   Korrekturhinweis erneut angefragt (`OLLAMAIL_LLM_STRUCTURED_OUTPUT_RETRIES`, Standard 2). Danach
   folgt `LLMOutputError`. Lehnt ein Server den Schema-Parameter ab (HTTP 400/422), fällt das
   Gateway für dieses Modell dauerhaft auf reines Prompting zurück. Das lässt sich pro Endpunkt
   auch fest einstellen (`structured_output=prompt`).
6. **Metriken:** Task, Endpunkt, Modell, Prompt-Version, Dauer, Token-Zahlen, Versuche und
   Fehlertyp. Prompts und Antworten werden **nie** erfasst. Fehlermeldungen enthalten keine
   Response-Bodies, weil manche Server die Anfrage darin zurückspiegeln.
7. **Prompt-Injection** (`app/ai/injection.py`, #170): Mail-Inhalte stehen in jedem Prompt in
   Datenblöcken mit pro Anfrage zufälligem Tag (`data_tag`, `data_block`); Absätze, die sich an
   einen KI-Assistenten oder Filter wenden, ersetzt `neutralize` vor dem Aufruf durch `[…]` und
   zählt sie ohne Inhalt (`ollamail_prompt_injection_suspected_total{feature}`). Was die Features
   daraus machen (Plausibilitätsregeln) und welche Kanäle nach außen wirken können:
   [PRIVACY.md, „Prompt-Injection im Detail“](PRIVACY.md#prompt-injection-im-detail).

**Modell-Evaluierung** (`backend/app/evals/`, #122): `uv run python -m app.evals --model … [--model …]`
misst Triage, Todos, Digest und RAG über einen synthetischen Datensatz (200 Mails DE/EN, 60 Fragen,
`app/evals/data/`) mit den Prompts und dem `LLMGateway` der Features (`EnvConfigResolver`); die
RAG-Stufe indexiert in einer zurückgerollten Transaktion einer separaten Datenbank und nutzt
`RagService`. Dazu kommt ein Durchlauf über Mails mit eingeschleusten Anweisungen (Kennzahl
„Injection befolgt“ je Stufe, #170). Bericht als Markdown und JSON, nur IDs und Zahlen. Läuft nicht in `ci-ok`, nur manuell
(Workflow „Model evals“). Ausführung und gemessene Ergebnisse:
[`operations/model-evals.md`](operations/model-evals.md).

Readiness: Mit `OLLAMAIL_LLM_READINESS_CHECK=true` prüft `/readyz` (Check `llm`), ob alle zugewiesenen
Modelle auf ihren Endpunkten verfügbar sind. Der Check ist standardmäßig aus, weil die API auch ohne
LLM nutzbar bleibt (Postfächer, Todos, Einstellungen). Mit `OLLAMAIL_LLM_PULL_MISSING_MODELS=true`
(Standard in `deploy/.env.example` für die gebündelten Ollama-Profile) lädt die API fehlende Modelle
beim Start im Hintergrund aus Ollama; ist der Endpunkt noch nicht erreichbar, fragt sie bis zu
sechsmal im Abstand von 10 s erneut.

#### Systemstatus für Admins (`backend/app/admin/system.py`, #139)

- `GET /api/admin/system/models`: je Task Endpunkt, Modell und Zustand (`installed`, `missing`,
  `unreachable`, `disabled` = Cloud-Endpunkt bei gesperrter Cloud) aus `LLMGateway.model_status()`;
  jeder Endpunkt wird einmal und höchstens 5 s lang nach seiner Modellliste gefragt. Dazu der
  letzte Download des Modells.
- `POST /api/admin/system/models/pull` (`{endpoint, model}`, 202): nur für Modelle, die einem Task
  auf einem Ollama-Endpunkt zugewiesen sind. Legt eine Zeile in `ai_model_pulls` an und reiht den
  Job `ai.pull_model` (Queue `default`, Lock je Download) ein; ein laufender Download wird nicht
  doppelt gestartet. Der Job streamt `POST /api/pull` und schreibt höchstens einmal pro Sekunde
  den Fortschritt (Bytes über alle Layer). Fehler nur als Code (`model_not_found`,
  `llm_unavailable`, `llm_timeout`, `pull_rejected`, `pull_failed`), kein automatischer Retry.
  Ohne Fortschritt seit 10 Minuten gilt ein Download als verloren und lässt sich neu starten.
- `GET /api/admin/system/overview`: Fakten für die Erste-Schritte-Checkliste (Anzahl Postfächer,
  eigener Digest an und Scheduler an, `OLLAMAIL_AUTH_PUBLIC_URL` gesetzt) und je Postfach
  Anzeigename, Besitzername, Sync-Phase mit Fehlercode und die Zahl ausstehender, laufender und
  fehlgeschlagener Verarbeitungsschritte (`processing.service.count_steps_by_mailbox`).
- `POST /api/admin/system/mailboxes/{id}/retry-failed`: setzt nur die fehlgeschlagenen Schritte
  des Postfachs auf `pending` (`processing.service.reset_failed_steps`) und reiht die Mails mit
  `Priority.REPROCESS` ein, also hinter neuen Mails.
- `POST /api/admin/system/mailboxes/{id}/include-older`: „Ältere Mails auch klassifizieren“
  (`processing.service.include_older`): setzt `include_older` des Postfachs, die übersprungenen
  Schritte auf `pending` und reiht die Mails mit `REPROCESS` ein. Die Übersicht zeigt dazu je
  Postfach `skipped_messages` und `include_older`.
- **UI:** Die Admin-Seite (`/admin`) zeigt Checkliste, Modellzustand mit Download-Button und
  Fortschritt sowie die Verarbeitung je Postfach. Die App-Shell zeigt unter dem Cloud-Hinweis
  dezente Hinweisleisten (`components/system-notices.tsx`): Admins sehen „Modell fehlt“ bzw.
  „Sprachmodell nicht erreichbar“ (Link zur Admin-Seite), alle Nutzer ein eigenes Postfach im
  Fehlerzustand (Link „Neu verbinden“ zu Einstellungen → Postfächer). Keine Modals. Auf Mobil
  fasst eine einzeilige, aufklappbare Leiste alle Hinweise zusammen; der Cloud-Hinweis lässt sich
  pro Sitzung ausblenden.

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

| Profil | Chat-Modell (Beispiel) | Embeddings (Beispiel) | Kontext | Gesamtfrist |
|---|---|---|---|---|
| `cpu` (Standard) | `qwen2.5:3b` | `bge-m3` | 8192 | 180 s |
| `gpu-consumer` | `qwen2.5:14b` | `bge-m3` | 16384 | 60 s |
| `gpu-server` | `qwen2.5:32b` | `bge-m3` | 32768 | 60 s |

### 3.3 TTS

Umgesetzt in `backend/app/ai/tts/`.

- Einstieg für Features: `TTSService.synthesize(text, lang=..., voice=..., target=..., formats=...)
  -> list[AudioFile]` (`get_tts()`), aufgerufen aus Jobs der Queue `tts`, nie im Request.
  Der Service wählt die Stimme (Nutzerwahl, falls sie zur Sprache passt und angeboten wird, sonst
  Standard je Sprache),
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
  per Muster validiert (`de_DE-thorsten-medium`), die Sprache ist Teil der ID. Wählbar sind nur
  Standard-, installierte und Allowlist-Stimmen (`OLLAMAIL_TTS_VOICE_ALLOWLIST`); geladen werden
  nur Standard- und Allowlist-Stimmen, nie auf Wahl eines Nutzers (#191).
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
- **Queues** `sync`, `llm`, `tts`, `ocr`, `default`, `push`. `OLLAMAIL_WORKER_QUEUES` wählt die
  Queues eines Worker-Prozesses; `llm` hat eigene Job-Slots (`OLLAMAIL_LLM_MAX_CONCURRENCY`), von
  denen das LLM-Gateway höchstens die im Admin-Bereich eingestellte Parallelität (Standard
  `OLLAMAIL_LLM_CONCURRENCY`) gleichzeitig an das Modell lässt; `ocr`
  (`OLLAMAIL_SEARCH_OCR_CONCURRENCY`) und `push` (`OLLAMAIL_NOTIFICATIONS_WEB_PUSH_CONCURRENCY`)
  haben ebenfalls eigene Slots, damit lange OCR-Jobs oder ein hängender Push-Dienst den Mail-Sync
  nicht blockieren; alle anderen Queues teilen sich `OLLAMAIL_WORKER_CONCURRENCY`. Ein Worker mit
  `default` arbeitet auch `push` ab (dort liefen Push-Jobs vor #185).
- **Task-Konventionen:** idempotent; Argumente nur IDs; Retry mit exponentiellem Backoff
  (`DEFAULT_RETRY`; Verarbeitungsschritte, deren LLM-Aufruf mit `LLMTimeoutError` endet, nur
  `OLLAMAIL_PROCESSING_LLM_TIMEOUT_ATTEMPTS` Versuche, Standard 2, danach `failed` mit Code
  `llm_timeout_error`); Lock-Keys pro Ressource (`resource_lock("mailbox", id)` als `lock`/`queueing_lock`);
  Periodic Tasks per `@app.periodic(cron=...)`. Task-Module werden in `TASK_MODULES` eingetragen.
- **Hängende Jobs:** `worker.retry_stalled_jobs` (alle 5 Minuten) reiht Jobs im Status `doing`
  erneut ein, deren Worker seit `OLLAMAIL_WORKER_STALLED_AFTER_SECONDS` keinen Heartbeat
  gesendet hat (Procrastinate `get_stalled_jobs`/`retry_job`); so gibt ein abgestürzter Worker
  auch die Locks seiner Jobs frei.
- **Housekeeping:** stündlicher Job `worker.remove_old_jobs` löscht erfolgreiche Jobs nach
  `OLLAMAIL_WORKER_JOB_RETENTION_HOURS` (Standard 24) und fehlgeschlagene, abgebrochene oder
  abgewiesene nach `OLLAMAIL_WORKER_FAILED_JOB_RETENTION_HOURS` (Standard 168). Jede Mail erzeugt
  rund sieben Jobs (ein `plan_message`, je Schritt ein `run_step`); ein Import von 100k Mails
  hinterlässt also ~700k Zeilen, die so nach einem Tag statt nach einer Woche verschwinden (#187).
  `mail.resume_deletions` (alle 15 Minuten) reiht das Entfernen markierter Postfächer erneut ein,
  falls dessen Job verloren ging (`mail.delete_mailbox`, siehe Postfach-API);
  `privacy.resume_user_deletions` (alle 15 Minuten) ebenso das Löschen markierter Nutzer
  (`privacy.delete_user`, #177).
  Aufbewahrungsfristen setzen `privacy.retention` (täglich), `digest.cleanup` (stündlich) und
  `rag.purge_conversations` (täglich) mit den Werten aus Admin → Aufbewahrung durch
  (`app/privacy/policy.py`, sonst Umgebung); `privacy.cleanup_exports` löscht abgelaufene Exporte.
- **Shutdown:** Bei SIGTERM nimmt der Worker keine neuen Jobs an; laufende Jobs haben
  `OLLAMAIL_WORKER_SHUTDOWN_TIMEOUT` Sekunden, dann endet der Prozess mit Exit-Code 0.
- **Events:** `publish(session, user_id, Event(...))` sendet per `pg_notify` beim Commit. Jeder
  API-Prozess hält eine `LISTEN`-Verbindung und verteilt an `GET /api/events` (SSE), gefiltert auf den
  angemeldeten Nutzer. Ein `Event` besteht nur aus `type`, `ids` und `status` (per Pattern validiert).
  Zustellung ist best effort: Nach einem Reconnect lädt der Client seine Daten neu. Pro Nutzer und
  Prozess sind höchstens `OLLAMAIL_EVENTS_MAX_STREAMS_PER_USER` Streams offen (darüber 429); alle
  `OLLAMAIL_EVENTS_SESSION_CHECK_INTERVAL` Sekunden prüft der Stream die Sitzung erneut (ohne sie zu
  verlängern) und endet nach Logout, Widerruf, Ablauf oder Deaktivierung (#191).
- **LLM-Last der API (#191):** Das Gateway der API begrenzt parallele Aufrufe auf
  `OLLAMAIL_LLM_API_CONCURRENCY` (weitere warten). `POST /rag/ask` und `POST /drafts/generate`
  belegen zusätzlich einen Platz des Nutzers (`app/ai/llm/user_limits.py`,
  `OLLAMAIL_LLM_API_USER_CONCURRENCY`) bis zum Ende des Streams; ohne freien Platz 429 mit
  `Retry-After` und `error_code` `llm_busy`. Die Suche fragt dann ohne Embedding (nur Volltext).
- **Keine Pool-Verbindung während langsamer Aufrufe** (#188): Wer in einer Session liest und dann
  das LLM, Embeddings oder einen anderen Netzwerkdienst aufruft, beendet vorher die
  Lesetransaktion mit `app.core.db.release_connection(session)`; geschrieben wird danach in einer
  neuen, kurzen Transaktion. Sonst hält jeder Job, der am Semaphor des LLM-Gateways wartet, eine
  Verbindung. Umgesetzt in Triage (`_decide`, Few-Shot-Ranking), Todo-Extraktion (alte Todos
  werden erst nach der Antwort ersetzt), Index (`index_message`, auch vor dem Textextrahieren der
  Anhänge) und Suche (Embedding der Anfrage vor den Kandidaten-Abfragen; gilt auch für RAG).
  `release_connection` committet (auch noch offene Änderungen), also vor Schreibzugriffen aufrufen,
  die mit dem Ergebnis des Aufrufs atomar sein müssen.
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
  `skipped` heißt „bewusst nicht ausgeführt“ (Altbestand, siehe unten); für `after` und das
  Event `message.processed` zählt es wie erledigt.
- **Altbestand (#141):** Schritte mit `recent_only=True` (Triage, Todos) laufen nur für Mails, die
  nach `now − OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS` eingegangen sind (`received_at`, sonst
  `sent_at`/`created_at`; `0` = alle). Ältere Mails bekommen nur die übrigen Schritte (Index,
  Embeddings); `plan` setzt die übersprungenen und die per `depends_on` davon abhängigen Schritte
  auf `skipped`. Bereits erledigte Schritte bleiben erledigt. Je Postfach hebt
  `processing_mailbox_settings.include_older` die Grenze auf (Admin-Systemstatus oder
  `python -m app.cli processing include-older <mailbox-id>`); `plan` setzt `skipped` dann wieder
  auf `pending`. Übersprungene Schritte gelten nicht als veraltet (`requeue_outdated`).
- **Automatische Wiederholung:** War der Grund vorübergehend (`LLMUnavailableError`,
  `ModelNotAvailableError`, begrenzt `LLMTimeoutError`), setzt `fail_step` `retry_at`;
  `processing.retry_failed` (alle 5 Minuten) setzt fällige Schritte wieder auf `pending`
  (`auto_retries` + 1) und reiht die Mails mit `REPROCESS` ein. Abstand wächst exponentiell
  (`OLLAMAIL_PROCESSING_AUTO_RETRY_*`), Erfolg oder Zurücksetzen setzt den Zähler zurück. Ein
  fehlendes Modell wird nicht sofort wiederholt, sondern nur so.
- **Circuit-Breaker:** Das LLM-Gateway des Workers (`app/ai/llm/circuit.py`) pausiert einen
  Endpunkt nach `OLLAMAIL_PROCESSING_LLM_BREAKER_THRESHOLD` Aufrufen in Folge, die an
  Nichterreichbarkeit scheitern, und wirft dann sofort `LLMCircuitOpenError`. `run_step` setzt
  den Schritt dann zurück auf `pending` (ohne einen Versuch zu verbrauchen) und plant einen neuen
  Job nach Ablauf der Pause. Nach der Pause prüft ein einzelner Aufruf den Endpunkt.
- **Zählen:** `count_steps_by_mailbox` liefert `StepCounts(pending, running, failed,
  retry_scheduled, skipped_messages)` je Postfach (Systemstatus; `skipped_messages` zählt Mails), `reset_failed_steps` setzt fehlgeschlagene
  Schritte eines Postfachs zurück. Offene Schritte (`pending`, `running`, `failed`) liest sie über
  den Teilindex `ix_message_processing_open` (Bedingung `OPEN_STEPS` wörtlich im SQL, nicht als
  Parameter); `skipped` gibt es für jede Mail außerhalb des Backfill-Fensters, das Zählen liest
  sie alle (bei 500k Mails ~0,5 s) und entfällt mit `skipped=False` (Metriken, #187).
- **Versionen:** Erhöht ein Schritt seine `version` (z. B. neuer Prompt), reiht der periodische Job
  `processing.requeue_outdated` (alle 10 Minuten, `OLLAMAIL_PROCESSING_REQUEUE_BATCH_SIZE` Mails,
  neueste zuerst) die betroffenen Mails ein; nur dieser Schritt läuft erneut. Derselbe Job holt Mails
  nach, die nie verarbeitet wurden. Damit er nicht alle 10 Minuten alle Mails liest (#187), merkt
  sich `processing_scan_state` die Schrittversionen, bei denen zuletzt keine Mail veraltet war, und
  den Zeitpunkt dieser Prüfung (`planned_before`). Solange die registrierten Schritte gleich bleiben,
  prüft `messages_to_requeue` nur Mails ab `planned_before − 1 h` (UUIDv7-IDs, Bereichsscan über den
  Primärschlüssel; die Stunde deckt Transaktionen ab, die nach einer späteren committen) per
  `NOT EXISTS` auf fehlende Zeilen. Eine noch ungeplante Mail hält `planned_before` fest, bis sie
  geplant ist. Neue oder geänderte Schritte und das Wiedereinschalten eines Postfachs
  (`set_mailbox_enabled`) setzen die Versionen zurück; dann prüft der Job wieder alle Mails.
  Mehrere Mails reiht `requeue_messages` in Batches ein (ein `INSERT` je 200 Jobs; ist eine
  davon schon eingereiht, wird dieser Batch einzeln eingereiht).
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
- **LLM** (`classify.py`, Prompt `triage@3` in `prompts.py`): `LLMGateway.complete_structured` mit
  Task `triage`, Temperatur 0. Das Antwortschema wird je Aufruf gebaut, die erlaubten
  Kategorie-Schlüssel stehen als `enum` darin. Ergebnis: Begründung (ein Satz in der UI-Sprache des
  Nutzers), Kategorie, Priorität 1–3 (1 = hoch) – die Begründung steht im Schema vorn, damit das
  Modell erst das entscheidende Merkmal nennt und dann wählt. Für die sichtbaren eingebauten
  Kategorien enthält der Prompt Entscheidungsregeln (`BUILTIN_RULES`, DE/EN), z. B. Antworten auf
  eigene Anfragen → „Warten auf“, Phishing/Gewinnspiele und Mails, die die Einordnung vorschreiben
  wollen → Spam (#158, Messung in `docs/operations/model-evals.md` §4.5). Die Mail steht als Daten
  in einem Block mit pro Anfrage zufälligem Tag, Text gekürzt auf `OLLAMAIL_TRIAGE_MAX_BODY_CHARS`.
- **Prompt-Injection** (#170): Absätze in Betreff, Text und Few-Shot-Beispielen, die sich an einen
  KI-Assistenten oder Filter wenden, entfernt `app.ai.injection.neutralize` vor dem Aufruf. Hatte
  die Mail solche Absätze, ist die Priorität mindestens 2 und die gespeicherte Begründung ein
  fester Prüfhinweis (`REVIEW_REASON`) statt der des Modells; „Wichtig“ und „Handlungsbedarf“
  werden zu Spam mit Priorität 3 (Spam-Regel der Triage, im Code durchgesetzt).
- **Lernen aus Korrekturen** (`feedback.py`): `PUT /triage/messages/{id}` speichert die Korrektur als
  Ergebnis und als Beispiel (`triage_feedback`). In den Prompt kommen bis zu
  `OLLAMAIL_TRIAGE_FEW_SHOT_EXAMPLES` Beispiele **nur desselben Nutzers** (Filter auf Nutzer *und* auf
  eigene Postfächer). Ausnahme Shared Mailboxes: Korrekturen wirken postfachweit, also für alle
  Nutzer des Postfachs, und dienen als Beispiele für genau dieses Postfach (Korrekturen aller
  seiner Nutzer, nur aus seinen Mails); persönliche Korrekturen fließen dort nie ein und
  umgekehrt. Shared Mailboxes nutzen nur die Org-Kategorien (eine Korrektur dort nimmt keine
  eigene Kategorie an, 422) und keine Absenderregeln. Gibt es mehr Kandidaten, wählt die Triage die ähnlichsten per Embedding
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
  Inbox nach Kategorie gruppiert (`GET /triage/inbox`, Posteingangsordner der lesbaren Postfächer),
  Absenderregeln, Write-back-Einstellung je Postfach. Für die UI (#21): `GET /triage/messages?ids=…`
  liefert die Triage vieler Mails auf einmal (sichtbare Listenzeilen), `GET /triage/inbox/messages`
  die Inbox als eine Liste sortiert nach Kategorie (Reihenfolge des Nutzers, ohne Kategorie zuletzt),
  Priorität und Datum, mit Filter auf eine Kategorie (`category=<id>|none`), Keyset-Seiten per
  `cursor` und – nur auf der ersten Seite – der Anzahl je Kategorie (`groups`) und `total`. Die Liste
  besteht aus Segmenten (Kategorie × Priorität), die nacheinander gelesen werden. Ein Segment
  triagierter Mails ist ein Bereich des Index `ix_triage_results_segment` (`mailbox_id`,
  `category_id`, `priority`, `sort_date DESC`, `message_id DESC`, #186); `mailbox_id` und
  `sort_date` sind Kopien aus `mail_messages`, gefüllt vom Trigger `triage_results_message_columns`
  und nachgeführt von `mail_messages_triage_sort_date`. Ein Segment mit zehn Mails kostet damit
  zehn Indexeinträge statt eines Laufs über das ganze Postfach. Die Unkategorisierten mit Priorität
  sind ein Bereich je Postfach und je Kategorie, die der Nutzer nicht sieht (gelöscht = `NULL`,
  ausgeblendet, Kategorie eines anderen Nutzers im Shared Mailbox), zusammengeführt wie mehrere
  Postfächer; welche Kategorien vorkommen, ermittelt ein Skip-Scan über denselben Index. Mit
  `unread` kann der Planer stattdessen bei `ix_mail_messages_unread` beginnen. Nur das Segment
  der noch nicht triagierten Mails läuft weiter über den Listenindex des Postfachs (es gibt keine
  Ergebniszeile, an der ein Index hängen könnte); bei wenigen untriagierten Mails unter vielen
  triagierten liest es entsprechend viel. Die erste Seite zählt die Segmente in einer Query ab den
  Posteingangsordnern (Kosten: Größe der Inbox, nicht des Postfachs), der Cursor merkt sich die
  nicht leeren, damit leere Segmente (z. B. ausgeblendete Kategorien) übersprungen werden.
  `GET /triage/inbox` braucht zwei Queries, unabhängig von der Zahl der Kategorien.
- **Events:** `message.triaged` (`message_id`, `mailbox_id`) an alle, die das Postfach lesen (Besitzer bzw. Nutzer eines Shared Mailbox), sobald der
  Schritt `triage` eine Kategorie gespeichert hat oder der Nutzer sie korrigiert. Die UI lädt daraufhin
  nur Labels und die Inbox nach Kategorie neu.
- **Benachrichtigungen** (#149, `backend/app/notifications/`): Opt-in je Nutzer unter
  Einstellungen → Benachrichtigungen (`GET/PUT /notifications/settings`, Tabelle
  `notification_settings`: `enabled`, `category_ids`, `show_subject`, `sound`; ohne Zeile alles
  aus). Nach dem Schritt `triage` prüft `notify_triaged`, ob die Mail neu ist – empfangen vor
  höchstens `OLLAMAIL_NOTIFICATIONS_MAX_AGE_MINUTES` (Standard 60, damit Initialimport, Rückstau
  und Neuverarbeitung niemanden fluten), ungelesen, im Posteingang, vom Vorfilter oder Modell
  klassifiziert (keine Korrektur) – und sendet dann `notification.message` (`message_id`,
  `mailbox_id`) an jeden Leser des Postfachs, der die Kategorie gewählt hat. Jede Mail höchstens
  einmal (`mail_notifications`, Unique auf `message_id`). Das Frontend (`MailNotifier`) holt über
  `GET /notifications/messages/{id}` nur Absender, Kategorie und – falls eingeschaltet – den
  Betreff und zeigt eine Browser-Notification (`tag` je Mail, `silent` außer mit `sound`), wenn
  ollamail offen, aber nicht im Vordergrund ist. Klick öffnet die Mail. Admin-Schalter:
  `OLLAMAIL_NOTIFICATIONS_ENABLED`.
- **Web Push** (#181, `backend/app/notifications/push.py`, `vapid.py`, `webpush.py`):
  Benachrichtigungen auch ohne offenen Tab. Eigener Admin-Schalter
  `OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ENABLED` (Standard aus), dazu das VAPID-Schlüsselpaar
  (`…_VAPID_PUBLIC_KEY`, `…_VAPID_PRIVATE_KEY`, `…_VAPID_SUBJECT`; erzeugt mit
  `python -m app.cli notifications vapid-keys`); ohne sie bleibt Web Push aus und Stufe 1
  funktioniert weiter. Der Nutzer schaltet es je Gerät ein (Einstellungen → Benachrichtigungen):
  Der Browser abonniert beim Push-Dienst seines Herstellers, `POST /notifications/push/devices`
  speichert das Abo in `push_subscriptions` (Endpoint und Schlüssel als `EncryptedJSON`,
  `endpoint_hash` = SHA-256 für „ein Browser, ein Eintrag“ – meldet sich ein anderer Nutzer im
  selben Browser an, gehört das Abo danach ihm; Browser, System und „mobil“ aus dem User-Agent als
  Bezeichnung; höchstens 20 Geräte je Nutzer). `GET /notifications/push` liefert Verfügbarkeit,
  Public Key und die eigenen Geräte, `DELETE /notifications/push/devices/{id}` entfernt eines
  (fremde: 404). Jedes Gerät gehört zur Sitzung, in der es registriert wurde (`session_id`,
  FK auf `auth_sessions` mit `ON DELETE CASCADE`, #185): Abmelden, Widerruf der Sitzung (auch
  durch den Admin) und Ablauf/Leerlauf (stündliches `auth.cleanup`) löschen es; bis dahin lässt
  der Job Geräte abgelaufener Sitzungen aus. Ein abonnierter Browser registriert sich beim
  nächsten Öffnen der App neu, gebunden an die dann aktive Sitzung. Beim Abmelden entfernt
  zusätzlich das Frontend das Gerät und beendet das Abo.
  Ablauf: Hat `notify_triaged` Empfänger, hängt der Triage-Schritt `enqueue_web_push` an
  `StepContext.after_commit` (läuft erst nach dem Commit des Schritts) – **ein** Job
  `notifications.web_push` je Mail für alle Empfänger (Queue `push` mit eigenen Slots,
  `OLLAMAIL_NOTIFICATIONS_WEB_PUSH_CONCURRENCY`, Standard 2; nur IDs; `queueing_lock` je Mail).
  Der Job liest die Geräte in einer kurzen DB-Sitzung, sendet ohne offene Sitzung parallel
  (höchstens 10 Geräte gleichzeitig) über einen gemeinsamen HTTP-Client je Worker-Prozess
  (Timeout: Verbindungsaufbau 3 s, Antwort 10 s) und schreibt das Ergebnis in einer zweiten kurzen
  Sitzung (`last_sent_at`, 404/410 löschen). Ein Circuit-Breaker je Push-Dienst
  (`PushServiceBreaker`) überspringt einen Dienst nach 5 Fehlversuchen in Folge 60 s lang (die
  Geräte kommen in den Folgejob) – ein gesperrter Egress kostet so nur wenige Timeouts statt
  einem je Gerät. Der Job verschlüsselt die Payload
  (`{"type":"notification.message","message_id","mailbox_id"}`) nach RFC 8291 (`aes128gcm`),
  signiert ein VAPID-JWT (RFC 8292, ES256) und sendet per HTTPS an den Endpoint. Erlaubt sind nur
  Hosts aus `OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ALLOWED_HOSTS` (Standard: FCM, Mozilla, Apple, WNS),
  keine Weiterleitungen – ein Endpoint kann den Worker also nicht auf interne Dienste lenken.
  404/410 löscht das Abo, 429/5xx/Verbindungsfehler wiederholt ein Folgejob nur für diese Geräte
  (höchstens 4 Versuche, 30/60/120 s), andere Fehler (z. B. 403 nach Schlüsselwechsel) werden nur
  mit Status geloggt. Der Service-Worker (`frontend/public/sw.js`) zeigt nichts, wenn ein Tab
  sichtbar und fokussiert ist; sonst holt er `GET /notifications/messages/{id}` (Cookie der
  Sitzung) und zeigt die Notification mit demselben `tag` wie der `MailNotifier` – ein Tab im
  Hintergrund ersetzt sie nur, es erscheint keine zweite. Ohne Sitzung oder bei Fehlern zeigt er
  einen neutralen Text ohne Details. Texte in der Sprache des Nutzers übergibt die App per
  `postMessage` (Cache `ollamail-strings-v1`, keine Maildaten). Klick fokussiert bzw. öffnet
  `/inbox?message=<id>`.
- **UI** (#21, `frontend/src/components/triage/`): Label in der Listenzeile, Begründungszeile über dem
  Thread, Korrektur per Klick, Command Palette oder `c` + Ziffer, Inbox-Ansicht nach Kategorie
  (`/inbox?category=all|<id>|none`), Einstellungen → Kategorien, Verwaltung → Kategorien der
  Organisation. Details: `frontend/README.md`.
- **Evaluierung:** `uv run python -m scripts.eval_triage --model qwen2.5:3b [--model …]` klassifiziert
  einen synthetischen, gelabelten Datensatz (`scripts/triage_eval_dataset.json`, DE/EN) und gibt die
  Genauigkeit je Modell aus (Kategorie, Priorität, Anteil Vorfilter, Fehlklassifikationen).
  Umfassender (größerer Datensatz, Konfusionsmatrix, Latenz): `python -m app.evals` (§3.2).

### 4.3 Todos

- Extraktion: Titel, Beschreibung, Fälligkeit (falls genannt), Priorität, Link zur Quell-Mail/zum Thread.
- Status: offen / erledigt / verworfen. Duplikaterkennung innerhalb eines Threads.

**Umsetzung (`backend/app/todos/`, #22):**

- **Schritt** `todos` (`steps.py`, Queue `llm`, `after=("triage",)`): läuft nicht für Mails, deren
  Triage-Kategorie in `OLLAMAIL_TODOS_SKIP_CATEGORIES` steht (Standard `newsletter,notification,spam`).
  Ohne Triage-Ergebnis (Triage nicht installiert, fehlgeschlagen) läuft er immer. Die Kategorie liest
  `extraction.message_category`; die Triage (#20) installiert ihren Lookup mit
  `extraction.set_category_lookup(...)`. Mails aus Shared Mailboxes ergeben Team-Todos (siehe
  unten), angesprochen mit dem Namen des Postfachs, Bezugstag in UTC.
  `OLLAMAIL_TODOS_EXTRACTION_ENABLED=false` schaltet den Schritt ab.
  Mails mit Absätzen, die sich an einen KI-Assistenten wenden (`app.ai.injection`, #170), gehen
  nicht ans Modell und ergeben keine Todos: Todos sind die einzige Ausgabe, die ohne Klick nach
  außen gelangen kann (Export im Modus `auto`).
- **Prompt** `todos_extract@3` (`app/ai/prompts/todos.py`, DE/EN) über `LLMGateway.complete_structured`
  mit `LLMTask.TODOS`. Das Modell bekommt Absender, Empfänger, Betreff, Text ohne Zitate, das
  Sendedatum (Wochentag + Datum in der Zeitzone des Nutzers), ob der Nutzer die Mail selbst
  geschrieben hat, und die offenen Todos des Threads (nummeriert).
- **Antwort** (`TodoExtraction`): zuerst `asks_user` (bittet die Mail den Nutzer ausdrücklich um
  etwas?), dann je Aufgabe Titel, Beschreibung, `due_phrase` (Frist wörtlich aus
  der Mail), `due_date` (Schätzung des Modells), Priorität `high|normal|low`, Konfidenz und optional
  `updates` (Nummer eines offenen Todos); dazu `done` (Nummern erledigter Todos). Zu lange Texte werden
  gekürzt, Konfidenz und Priorität normalisiert, statt die Antwort zu verwerfen. Das JSON-Schema
  koppelt die Liste an `asks_user` (`false` → leere Liste, `true` → 1 bis 5 Einträge); Endpunkte mit
  nativer strukturierter Ausgabe setzen das als Grammatik durch. Ohne diese Grenze hängten kleine
  Modelle Einträge an, bis das Tokenlimit erreicht war (#158).
- **Nachbearbeitung** (`plan_extraction`, ohne DB):
  - Aufgaben unter `OLLAMAIL_TODOS_MIN_CONFIDENCE` werden verworfen, ebenso alle neuen Aufgaben,
    wenn `asks_user` `false` ist. Von den übrigen bleiben die `OLLAMAIL_TODOS_MAX_PER_MAIL`
    (Standard 3) mit der höchsten Konfidenz.
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
- **Team-Todos** (#34): Todos eines Shared Mailbox gehören dem Postfach (`user_id IS NULL`), alle
  seine Nutzer sehen und bearbeiten sie, solange sie es lesen dürfen. `assignee_id` weist sie einer
  dieser Personen zu (`PATCH /todos/{id}`, `{"assignee_id": …}`; sonst 422 `invalid_assignee`,
  eigene Todos: `not_assignable`); wird die Person gelöscht, ist das Todo wieder offen
  (`SET NULL`). Ein manuelles Todo zu einer Mail eines Shared Mailbox ist ein Team-Todo, das dem
  Ersteller zugewiesen ist. Der Digest nimmt nur die eigenen und die zugewiesenen auf.
- **Modell** `Todo` (`models.py`, Tabelle `todos`): Nutzer, Quelle (Postfach, Mail, Thread),
  Status `open|done|dismissed`, `is_manual`, `is_edited`, Konfidenz, `done_suggested`,
  `completed_at` und `external_refs` (JSON, für den Export in #40).
- **API** (`/todos`, eigene und Team-Todos lesbarer Shared Mailboxes): `GET /todos` (Filter `status` mehrfach, `mailbox_id`,
  `due_before`, `due_after`; früheste Fälligkeit zuerst, ohne Fälligkeit zuletzt; `limit`/`offset`),
  `POST /todos` (manuell, optional mit `message_id` einer lesbaren Mail), `GET|PATCH|DELETE /todos/{id}`.
  `PATCH` bearbeitet Felder und den Status (`done` setzt `completed_at`, jede Statusänderung löscht
  den Vorschlag); `done_suggested: false` verwirft nur den Vorschlag.
- **Evaluierung:** `python -m app.todos.evaluation [--model NAME ...]` läuft mit dem echten Prompt
  gegen den konfigurierten Endpunkt über `app/todos/eval_cases.json` (synthetische DE/EN-Mails mit
  erwarteten Todos, Fristen, Updates und Erledigt-Vorschlägen) und gibt je Modell Precision, Recall
  und Trefferquote der Fristen aus. Neue Fälle im selben Format ergänzen. Über den großen
  Datensatz mit Fuzzy-Match der Titel: `python -m app.evals --stage todos` (§3.2).
- **Export** (`backend/app/todos/export/`, #40): Aufgaben landen in der Aufgabenliste, mit der
  der Nutzer ohnehin arbeitet. Umgesetzt sind CalDAV (VTODO), Microsoft To Do (Graph, #101)
  und Google Tasks (#102).
  - **Admin-Opt-in:** `OLLAMAIL_TODOS_EXPORT_SINKS` (Standard leer = aus) nennt die Ziele, die
    Nutzer verbinden dürfen. Ziele, die der Admin später entfernt, werden nicht mehr abgeglichen.
  - **Interface** `TodoSink` (`base.py`): `list_task_lists`, `push` (idempotent je `uid`),
    `update`/`complete` (mit ETag, sonst `SinkConflictError`), `delete`, `changes` (Status der
    bekannten Aufgaben, die sich im Ziel geändert haben oder dort gelöscht wurden),
    `updated_config` (rotierte OAuth-Tokens oder Abgleich-Stand, die der Abgleich speichert,
    solange der Nutzer nicht neu verbunden hat). Fehler sind `SinkError`-Codes ohne Servertexte. `registry.FACTORIES` ordnet Zieltypen Implementierungen
    zu; ein neuer Dienst ist eine Klasse plus ein Eintrag.
  - **CalDAV** (`caldav.py`, `ical.py`, `httpx` ohne CalDAV-Bibliothek): Discovery über
    `current-user-principal` und `calendar-home-set` (auch `/.well-known/caldav`), angeboten
    werden nur Kalender mit VTODO. Eine Aufgabe ist `<Liste>/<todo-id>.ics`; Anlegen mit
    `If-None-Match: *`, Ändern mit `If-Match`. Der Statusabgleich holt per `PROPFIND` die ETags
    der Liste und per `calendar-multiget` nur die geänderten Aufgaben. Die Beschreibung enthält
    den Link zur Mail (`<OLLAMAIL_AUTH_PUBLIC_URL bzw. Origin beim Verbinden>/inbox?message=<id>`,
    auch als `URL`). Anfragen gehen nur an den eingetragenen Server; `https` ist Pflicht
    (`OLLAMAIL_TODOS_EXPORT_ALLOW_HTTP` nur für Tests), keine DTDs in Antworten.
    **Zielprüfung** (#189): dieselbe wie bei IMAP/SMTP (`app/core/network.py`), über einen
    eigenen `httpx`-Transport (`app/core/http_guard.py`) für jede Verbindung, auch nach einer
    Weiterleitung. Interne Adressen nur mit Eintrag in
    `OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS`; verbunden wird mit der geprüften Adresse,
    `Host`, SNI und Zertifikatsprüfung behalten den Namen (kein DNS-Rebinding). Proxy-Variablen
    aus der Umgebung gelten für diese Verbindungen nicht. Nach außen gehen nur grobe Fehlercodes
    (`unavailable` – auch für abgelehnte Ziele und Timeouts –, `auth_failed`, `not_found`,
    `not_caldav`), keine HTTP-Statuscodes; so taugt das Verbinden nicht als Portscanner.
  - **Microsoft To Do** (`mstodo.py`, `mstodo_router.py`, Details:
    [`providers/microsoft365.md`](providers/microsoft365.md) §11): eigener OAuth-Flow mit
    `Tasks.ReadWrite` (Entra-App und Token-Erneuerung der Graph-Postfächer), Tokens verschlüsselt
    in der Konfiguration; Aufgaben mit `linkedResources` (Todo-ID, Link zur Mail), Änderungen mit
    `If-Match`, Statusabgleich per Delta Query. Ziele mit OAuth (`registry.OAUTH_SINKS`) lehnt
    `PUT /todo-export` ab (`oauth_required`).
  - **Google Tasks** (`gtasks.py`, `gtasks_connect.py`, Details in
    [`providers/gmail.md` §8](providers/gmail.md#8-google-tasks-aufgaben-export-102)): Tasks API
    v1 über `GoogleApiClient` und OAuth-Client des Gmail-Providers (Scope `tasks`, eigener
    Connect-Flow `POST /todo-export/gtasks/oauth/start`, `GET …/callback`, Refresh-Token
    verschlüsselt in `config`). Ein neues Ziel exportiert in die Standardliste, eine andere
    wählt der Nutzer danach (`GET /todo-export/lists`, `PATCH /todo-export {"list_id"}`; beides
    für alle Ziele, rotierte Tokens werden dabei gespeichert). Keine Priorität, „verworfen“
    wird „erledigt“; Duplikate verhindert eine Markierung `[ollamail:<id>]` in `notes`, nach
    der `push` zuerst sucht.
  - **Einstellungen** je Nutzer (`todo_export_targets`, höchstens eine Zeile): Ziel,
    Zugangsdaten (`EncryptedJSON`), Liste, Modus `auto` (alle offenen Aufgaben, neue sofort)
    oder `manual` (nur einzeln exportierte). API `GET|PUT|PATCH|DELETE /todo-export`,
    `POST /todo-export/lists` (Verbindung prüfen, Listen holen), `POST /todo-export/sync`,
    `POST /todo-export/todos/{id}` (einzeln exportieren). Verbinden, Ändern und Trennen stehen
    im Audit-Log (`todo_export.changed`). Ein anderer Server bzw. ein anderes Konto beginnt neu.
  - **Verweise** in `Todo.external_refs["caldav"]`: Ziel, Liste, Remote-ID, ETag, Zustand
    (`pending`, `synced`, `error`, `removed`) und `synced_at`. Die API liefert daraus
    `export_state`. `synced_at` ist nach jedem Schreiben gleich `todos.updated_at`; jede spätere
    Änderung (API, Extraktion) macht sie ungleich. So findet der Abgleich geänderte Aufgaben
    ohne eigenes Änderungsprotokoll.
  - **Abgleich** (`service.sync_target`, Job `todos.export_sync`, Queue `sync`, Argument nur die
    Ziel-ID, Lock je Ziel): neue, angeforderte und geänderte Aufgaben werden gesendet; alle
    `OLLAMAIL_TODOS_EXPORT_POLL_MINUTES` (Standard 15) kommt der Status aus dem Ziel zurück
    (erledigt ↔ `COMPLETED`, verworfen ↔ `CANCELLED`). **Konflikte:** Für den Status gewinnt die
    spätere Änderung (`LAST-MODIFIED` gegen `updated_at`); Titel, Beschreibung, Fälligkeit und
    Priorität kommen immer aus ollamail. Jede Aufgabe wird in einer eigenen kurzen Transaktion mit
    Bedingung auf `updated_at` geschrieben, eine gleichzeitige Änderung des Nutzers geht nie
    verloren. Im Ziel gelöschte Aufgaben werden nicht erneut exportiert, außer der Nutzer
    fordert es an. In ollamail gelöschte Aufgaben werden im Ziel gelöscht (vorgemerkt in
    `pending_deletions`). Fehler des Ziels (`auth_failed`, `unavailable`, `list_not_found`)
    stehen am Ziel und werden erst mit der nächsten Statusprüfung wiederholt.
  - **Auslöser:** `POST|PATCH|DELETE /todos` stoßen den Abgleich nach dem Commit an; der Job
    `todos.export_schedule` (jede Minute) findet außerdem neue Aufgaben aus der Extraktion und
    fällige Statusprüfungen. Das Ereignis `todo.exported` aktualisiert die UI.
  - **UI:** Einstellungen → Aufgaben-Export (Ziel wählen, verbinden, Liste und Modus wählen;
    vorher steht, welche Daten übertragen werden), in der Aufgabenliste ein Symbol je Aufgabe
    (exportiert, wartet, fehlgeschlagen, im Ziel gelöscht) und im Modus „Manuell“ die Aktion
    „Exportieren“.

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
     Jede Mail steht in einem Datenblock mit pro Anfrage zufälligem Tag (`digest_map@2`),
     Absätze an KI-Assistenten sind vorher durch `[…]` ersetzt (#170).
  2. *Condense:* Passen die Notizen nicht in ein Kontextfenster, werden Gruppen zu weniger
     Notizen zusammengefasst (Referenzen bleiben erhalten), bei Bedarf mehrfach.
  3. *Reduce:* aus den Notizen der gesprochene Hauptteil mit `[n]`-Referenzen (Prompt mit
     Beispiel). Erfundene Referenzen, Überschriften, Listen und `<think>`-Blöcke werden entfernt,
     Varianten wie `[ 3 ]`, `[^3]` oder `[2; 7]` zu `[3]` bzw. `[2, 7]` vereinheitlicht. Bleibt
     keine gültige Referenz übrig, fragt der Code einmal mit Korrekturhinweis nach (#171); eine
     unbrauchbare Antwort wird durch die Notizen ersetzt.
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
  `GET /api/digests/voices` listet die wählbaren Stimmen (installierte, Standardstimme je
  Sprache und `OLLAMAIL_TTS_VOICE_ALLOWLIST`, mit `default`/`installed`); eine andere Stimme lehnt
  `PATCH /api/digests/settings` mit 422 (`unknown_voice`) ab.
- **Web-UI** (#29, `frontend/README.md`): Seite `/digest` mit Player (Media Session API,
  Tastatur), Transkript mit Links auf die Mails und Archiv; Einstellungen und Feed-URL (einmalig
  angezeigt, mit QR-Code) unter `/digest/settings`.
- **Podcast-Feed:** `POST /api/digests/feed` erzeugt ein zufälliges Token (256 Bit) und liefert
  einmalig die URL `/api/feeds/{token}.xml`; gespeichert wird nur der SHA-256-Hash. Erneutes
  `POST` ersetzt, `DELETE /api/digests/feed` widerruft das Token; alte URLs liefern danach 404.
  Der Feed (RSS 2.0 mit iTunes-Namespace, `itunes:block`) enthält fertige Digests mit Audio;
  die Audio-URLs `/api/feeds/{token}/{digest_id}.mp3` sind ebenfalls nur mit Token abrufbar und
  unterstützen `HEAD` und Range-Requests. Feed-Routen sind nicht Teil des OpenAPI-Schemas.
- **Aufbewahrung:** Der stündliche Job `digest.cleanup` löscht Digests älter als die Frist aus
  Admin → Aufbewahrung (sonst `OLLAMAIL_DIGEST_RETENTION_DAYS`, Standard 30) samt Dateien, Digests eines inzwischen
  gelöschten Postfachs, Dateien ohne Digest (z. B. gelöschter Nutzer) und markiert seit Stunden
  hängende Digests als fehlgeschlagen.

### 4.5 RAG („Frag deine Inbox“)

- Chunking von Mail-Text und extrahiertem Anhangstext (PDF, DOCX, TXT, HTML; gescannte PDFs und Bilder per OCR).
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
  `timeout`, `unreadable`, `encrypted`, `unsupported`, `missing`, `ocr_pending`).
- **OCR** (#98, Tesseract, lokal): `OLLAMAIL_SEARCH_OCR_MODE` `off` / `pdf` (Standard) / `all`.
  Der Schritt `index` erkennt nur, ob ein PDF Seiten ohne Textlayer, aber mit Bild hat (Modus
  `detect` im Kindprozess, Status `ocr_pending`), indiziert den vorhandenen Textlayer sofort und
  stellt den Job `search.ocr_attachment` auf die Queue `ocr` (eigene Job-Slots
  `OLLAMAIL_SEARCH_OCR_CONCURRENCY`, niedrigste Priorität; gleicher Lock wie der `index`-Job der
  Mail, startet also erst nach dessen Commit). Der Job liest das Anhang erneut im selben
  isolierten Kindprozess (Modus `run`): Textlayer zuerst, nur Seiten ohne Text werden mit
  `pypdfium2` als Graustufenbild (300 dpi, höchstens 40 Mpx) gerendert und an `tesseract`
  (Kind des Kindprozesses, erbt leere Umgebung und Limits, Bild über stdin, Text über stdout)
  gegeben, höchstens `OLLAMAIL_SEARCH_OCR_MAX_PAGES` Seiten. Bilder (PNG, JPEG, TIFF; nicht
  inline) nur im Modus `all`. Bei Timeout (`OLLAMAIL_SEARCH_OCR_TIMEOUT`) wird die ganze
  Prozessgruppe beendet. Ergebnis: Die Chunks des Anhangs werden ersetzt, mit Quelle
  `attachment_ocr` („Anhang (OCR)“ in Treffern und Zitaten); Vektoren ergänzt
  `search.fill_embeddings`. Der OCR-Job ruft nie das LLM auf. Fehler (`timeout`, `ocr_failed`,
  `ocr_unavailable`) nur als Statuscode; der Textlayer bleibt dann im Index.
- **Tabellen:** `search_chunks` (Text, `ts_config` `german`/`english`/`simple` aus der erkannten
  Sprache, generierte `tsvector`-Spalte mit GIN-Index), `search_embeddings` (`chunk_id`, `model`,
  `embedding halfvec(n)` mit HNSW-Index, Kosinus; 16 Bit je Dimension, #164),
  `search_index_state` (aktives Modell). `n` kommt
  aus `OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS`; zur Laufzeit gilt die Länge der Datenbankspalte.
- **Embeddings** über `LLMGateway.embed` (Aufgabe `embeddings`) in Batches
  (`OLLAMAIL_SEARCH_EMBED_BATCH_SIZE`, optional Pause), auf der Queue `llm` mit deren
  Parallelität. Schlägt das Einbetten fehl, werden die Chunks ohne Vektor gespeichert; der Job
  `search.fill_embeddings` ergänzt sie. Damit er nicht alle 5 Minuten alle Chunks liest
  (#187, #224), trägt jedes Speichern von Chunks ohne Vektor des aktuellen Modells (Einbetten
  fehlgeschlagen, OCR) deren IDs in `search_embedding_backlog` ein und erhöht
  `search_index_state.fill_requested`. Der Job arbeitet nur, wenn dieser Zähler von
  `fill_checked` abweicht (Wert, bei dem zuletzt nichts fehlte) oder ein Modellwechsel läuft, und
  nimmt die Chunks dann aus dem Backlog (höchste UUIDv7 = neueste zuerst, `LIMIT`) – kein
  Abgleich aller Chunks mit ihren Vektoren. Vektoren früherer Modelle löscht er mit
  `model < aktuell OR model > aktuell` (nutzt den Index auf `model`, anders als `!=`).
- **Modellwechsel:** Vektoren tragen ihr Modell. Weicht das konfigurierte Modell von
  `search_index_state.backlog_model` ab (Wechsel, erster Lauf nach dem Upgrade), baut
  `search.fill_embeddings` den Backlog einmal aus allen Chunks ohne Vektor dieses Modells neu auf
  und rechnet sie batchweise; Anfragen nutzen bis zum Abschluss das alte Modell
  (`LLMGateway.embed(model=...)`). Vor dem Umschalten prüft ein zweiter Abgleich, ob Jobs
  inzwischen Chunks nur mit dem alten Modell gespeichert haben; dann wird umgeschaltet und
  aufgeräumt.
  Dimensionswechsel: `python -m app.cli search resize` (`docs/OPERATIONS.md` 3.7).
- **Suche:** `search(session, user_id, query, filters, embedder=..., settings=...)` in
  `app.search.service` liefert Chunks (für #25) oder mit `per_message=True` die beste Stelle je
  Mail (klassische Suche). Volltext: `websearch_to_tsquery` in allen drei Konfigurationen,
  ODER-verknüpft, Rang `ts_rank_cd`. Gibt es mehr als `OLLAMAIL_SEARCH_TEXT_RANK_WINDOW` Treffer
  (eine auf `WINDOW + 1` begrenzte Zählung prüft das), werden nur die neuesten so vielen
  (`sort_date`) gerankt, weil das Ranking den `tsvector` jedes Treffers liest und häufige Wörter
  Hunderttausende Chunks treffen (#224); ältere Treffer findet weiterhin die Vektorsuche. Vektor: Kosinus-Distanz über den HNSW-Index. Je Index
  `OLLAMAIL_SEARCH_CANDIDATES` Kandidaten, Fusion per Reciprocal Rank Fusion
  (`Σ 1/(k + rang)`, `OLLAMAIL_SEARCH_RRF_K`). Ist kein Embedding möglich, nur Volltext.
  Filter: Postfächer, Ordner, Absender, Zeitraum, Quelle (Mail/Anhang, mit oder ohne OCR).
- **Zugriff:** Jede Abfrage enthält `mailbox_id IN (accessible_mailbox_ids(user_id))` aus
  `app.mail.access`, der einzigen Stelle dieser Regel (eigene und zugewiesene Shared Mailboxes).
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
  Absätze, die sich an einen KI-Assistenten wenden, ersetzt `render_blocks` durch `[…]` (#170).
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
| `POST /rag/ask` | Frage stellen (`question`, optional `conversation_id`, `filters`). Antwort als `text/event-stream`: `start` (IDs), `filters` (angewandte Filter), `sources` (nummerierte Quellen mit Mail-ID und Ausschnitt), `token`…, dann `done` (`status`, `citations`, `ttft_ms`) oder `error` (`code`: `llm_unavailable` (Server nicht erreichbar), `llm_timeout` (Gesamtfrist bzw. Lese-Timeout überschritten, `LLMTimeoutError`: Server erreichbar, aber zu langsam), `llm_cloud_disabled`, `llm_error`, `internal`). POST, damit die Frage nicht in URLs/Access-Logs landet; Clients lesen den Stream per `fetch` |
| `GET /rag/conversations` | Eigene Gespräche, zuletzt genutzte zuerst |
| `GET/DELETE /rag/conversations/{id}` | Gespräch mit Fragen, Antworten und Zitaten; löschen. Fremde Gespräche verhalten sich wie nicht vorhandene (404) |
| `DELETE /rag/conversations` | Alle eigenen Gespräche löschen |

### 4.6 Antwortentwürfe (`backend/app/drafts/`, #92)

Auf Wunsch erzeugt das lokale Modell einen Antwortentwurf zu einer Mail; der Nutzer bearbeitet
ihn und sendet ihn über das Postfach, aus dem die Mail stammt. **Nichts wird automatisch
gesendet** – Senden ist immer ein eigener Request des Autors.

- **Datenmodell:** `reply_drafts` (Nutzer, Postfach – beide `ON DELETE CASCADE` –, Thread und
  beantwortete Mail – `SET NULL` –, Status `draft|sent|discarded`, Empfänger, Betreff, Text,
  Anweisung, Sprache, Modell, Prompt-Version, Versandzeitpunkt, `Message-ID` der gesendeten
  Mail) und `reply_draft_settings` (Signatur, Stilbeispiele an/aus; am Nutzer).
- **Zugriff:** Ein Entwurf ist nur für seinen Autor sichtbar und nur, solange er das Postfach
  lesen darf (`accessible_mailbox_ids`, bei jeder Anfrage in SQL); sonst 404. Erzeugen braucht
  Leserecht auf die Mail, Senden zusätzlich `MailboxPermission.SEND` (nur Eigentümer; auch mit
  `act`-Zuweisung wird aus Shared Mailboxes nicht gesendet → 403 `read_only`).
- **Generierung** (Aufgabe `reply_draft` im LLM-Gateway, Modell im KI-Admin zuweisbar,
  Prompt `reply_draft@1`, gestreamt per SSE): Kontext ist der Thread bis zur beantworteten Mail
  (höchstens `OLLAMAIL_DRAFTS_THREAD_MESSAGES` Mails, je `OLLAMAIL_DRAFTS_MESSAGE_CHARS` Zeichen
  aus `body_main`), die optionale Anweisung des Nutzers („kurz zusagen“), die Sprache der Mail
  und der Name des Nutzers. Die Signatur wird nach der Generierung angehängt, nicht vom Modell
  geschrieben. Optional (pro Nutzer abschaltbar, instanzweit über
  `OLLAMAIL_DRAFTS_STYLE_EXAMPLES=0`) bekommt das Modell einige eigene gesendete Mails derselben
  Sprache als Stilbeispiele – nur aus Ordnern mit Rolle „Gesendet“ in Postfächern, die dem
  Nutzer gehören, und nur mit seiner Postfachadresse als Absender; nie aus Shared Mailboxes oder
  von anderen Nutzern.
- **Prompt-Injection:** wie bei RAG – Mails und Stilbeispiele stehen nur in Datenblöcken mit
  zufälligem Tag pro Anfrage (`<mail-3f9a… n="2" latest="true">`), der System-Prompt erklärt sie
  zu nicht vertrauenswürdigen Daten. Nur die Anweisung des Nutzers steht außerhalb.
  Absätze, die sich an einen KI-Assistenten wenden, ersetzt `render_blocks` durch `[…]` (#170). Das Modell
  hat keine Tools; seine Ausgabe ist nur Text in einem Entwurf, den der Nutzer vor dem Senden
  sieht. Empfänger bestimmt nie das Modell, sondern `app/mail/compose.py` aus den Kopfzeilen.
- **Versand** (`sending.py`): Entwurfszeile gesperrt (`FOR UPDATE`), damit ein Doppelklick
  nicht doppelt sendet; Quelle über `compose.build_reply` (Plain Text, optional mit zitierter
  Original-Mail), dann `MailProvider.send`. Erfolg → Status `sent`, Audit-Eintrag `mail.sent`
  (Akteur, Postfach-ID, Entwurfs- und Mail-ID, Anzahl Empfänger – keine Adressen, kein Betreff,
  kein Text). Fehler lassen den Entwurf offen: 502 mit Code (z. B. `recipients_refused`,
  `send_not_permitted`), 503 bei nicht erreichbarem Server, 409 für Konfigurationsfehler.
- **Aufbewahrung:** Der tägliche Job `drafts.purge` löscht Entwürfe, die seit
  `OLLAMAIL_DRAFTS_RETENTION_DAYS` (Standard 30, 0 = nie) nicht geändert wurden.
- **UI** (#93): Editor unter dem Thread, Übersicht `/drafts`; Senden nur per eigener Aktion, ein
  unveränderter Vorschlag verlangt eine zweite Bestätigung. Details: `frontend/README.md`.

| Endpunkt | Zweck |
|---|---|
| `POST /drafts/generate` | Entwurf erzeugen (`message_id`, optional `instruction`, `reply_all`, `draft_id` zum Neuschreiben). `text/event-stream`: `start` (`draft_id`), `token`…, dann `done` (gespeicherter Entwurf, `ttft_ms`) oder `error` (`code` wie bei `POST /rag/ask`: `llm_unavailable`, `llm_timeout`, `llm_cloud_disabled`, `llm_error`, `internal`) |
| `POST /drafts` | Entwurf ohne Modell anlegen (Empfänger und Betreff aus der Mail) |
| `GET /drafts` | Eigene Entwürfe (Filter `message_id`, `status`) |
| `GET/PATCH/DELETE /drafts/{id}` | Lesen, bearbeiten (Text, Betreff, Empfänger, Allen antworten, Zitat), endgültig löschen |
| `POST /drafts/{id}/send` | Senden |
| `POST /drafts/{id}/discard` | Verwerfen (bleibt bis zum Ablauf der Aufbewahrung) |
| `GET/PUT /drafts/settings` | Signatur, Stilbeispiele |

## 5. Auth & Mandantenmodell

- **Eine Organisation pro Instanz.** Rollen: `admin`, `user` (erweiterbar, z. B. `auditor`).
- **Bootstrap:** Der erste Login/Registrierung einer frischen Instanz wird Admin. Danach ist die lokale
  Registrierung standardmäßig deaktiviert. Absicherung gegen Race Conditions (DB-Lock) und optional ein
  Setup-Token aus den Logs/Env (`OLLAMAIL_SETUP_TOKEN`).
- **Identity-Provider** (im Admin-UI konfigurierbar, verschlüsselt gespeichert):
  - OIDC generisch + Presets: Microsoft Entra ID, Google, Keycloak/Authentik
  - GitHub (OAuth2), optional eingeschränkt auf Organisationen/Teams
  - LDAP / Active Directory (Bind + Suche, StartTLS/LDAPS, Gruppen)
  - SAML 2.0 (SP-initiiert): Entra ID, AD FS, Okta, Keycloak, generisch
- **Just-in-Time-Provisioning**: Nutzer wird beim ersten Login angelegt; Rollen über Gruppen-Mapping
  (Entra-Gruppen, LDAP-Gruppen, GitHub-Teams, SAML-Gruppenattribut); Domain-Allowlist.
- **Sessions**: serverseitig in Postgres, `HttpOnly`/`Secure`/`SameSite=Lax`-Cookie, CSRF-Schutz.
  Keine JWTs im Browser-Storage.
- **SCIM-Provisioning** (Entra ID, Okta): IdP legt Nutzer an, deaktiviert/löscht sie und pflegt Gruppen.
- Optional später: TOTP/WebAuthn für lokale Accounts.

### Umsetzung (`backend/app/auth/`, `backend/app/users/`)

**Datenmodell:** `users` (E-Mail normalisiert und eindeutig, Anzeigename, Rolle `admin|user`,
Sprache, Zeitzone, aktiv), `auth_identities` (`provider`, `subject`, `user_id`; ein Nutzer kann
mehrere Identitäten haben; lokal: `provider=local`, `subject` = Nutzer-ID, Argon2id-Hash),
`auth_sessions`, `auth_rate_limits`, `auth_identity_link_notices` (#208). Alles hängt per `ON DELETE CASCADE` am Nutzer.

**Provider-Interface** (`app/auth/providers/base.py`): Ein Provider beweist nur, wer jemand ist,
und liefert eine `VerifiedIdentity(provider, subject, email, display_name, groups,
email_verified)`. `PasswordAuthProvider.authenticate(login, password)` für lokale Konten und
LDAP (#32), `RedirectAuthProvider.authorization_url(...)`/`complete(...)` (mit `state`, `nonce`
und PKCE-`code_verifier`) für OIDC (#30), GitHub (#31) und SAML (#94). Sperre, Session und Rollenprüfung
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
des Providers; `None` heißt, der Provider verwaltet keine Rollen. Ist die zentrale
Rollen-Zuordnung (#33) aktiv, bestimmt sie die Rolle für alle Provider gleich
(`app.auth.policy.resolve_role`); der letzte aktive Admin wird dabei nie herabgestuft. Kontoanlage und Rollenwechsel
landen im Audit-Log. Fehler sind `ProvisioningError` (ein `ProblemError` mit statischem `code`).

**Externe Logins im Browser** (`app/auth/redirect_flow.py`): Der Flow für Redirect-Provider
(verschlüsseltes Einmal-Cookie mit `state`, `nonce`, PKCE-Verifier; Fehler als Redirect auf
`/login?error=<code>`) ist providerunabhängig; GitHub (#31) und SAML (#94) nutzen ihn mit.

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

**SAML 2.0** (`app/auth/providers/saml/`, Anleitung: [`auth/saml.md`](auth/saml.md)): SP-initiiert,
HTTP-Redirect hin, HTTP-POST zurück an einen gemeinsamen ACS (`/api/auth/saml/acs`, von der
CSRF-Prüfung ausgenommen; der Provider steht im Flow-Cookie, dessen `SameSite=None` den
Cross-Site-POST erlaubt). Validierung mit python3-saml (strict, xmlsec, kein DTD/XXE, kein SHA-1),
zusätzlich Pflicht auf `InResponseTo` (AuthnRequest-ID aus dem Flow-`nonce`), exakte
`Destination`/`Recipient`, Audience und Replay-Schutz über `auth_saml_assertions` (SHA-256 der
Assertion-ID, in Postgres für alle Instanzen). Provider in `auth_saml_providers` (IdP-Metadaten
per URL oder Upload, Admin-API unter `/api/admin/auth/saml`), Presets nur für Attributnamen.

**LDAP / Active Directory** (`app/auth/providers/ldap/`, Details: [`auth/ldap.md`](auth/ldap.md)):
Verzeichnisse stehen in `auth_ldap_directories` (Einstellungen als JSONB, Bind-Passwort
verschlüsselt) und werden über `/api/auth/ldap/directories` (nur Admins) gepflegt und getestet.
Login über `POST /api/auth/login/ldap/{name}` mit denselben Rate-Limits wie der lokale Login.

**Bootstrap:** `GET /api/setup/status` → `{"initialized": bool}`. `POST /api/setup` legt den ersten
Admin an und meldet ihn an. Voraussetzung ist der Setup-Token (`OLLAMAIL_SETUP_TOKEN` oder per
HKDF aus `OLLAMAIL_SECRET_KEY` abgeleitet, auf allen API-Instanzen gleich, beim Start geloggt
und per `python -m app.cli setup-token` abrufbar; ein eigener Token braucht mindestens 32 Zeichen).
Jeder Versuch zählt gegen das IP-Limit des Logins (`OLLAMAIL_AUTH_IP_MAX_ATTEMPTS`). Ein transaktionaler Advisory Lock
(`pg_advisory_xact_lock`) serialisiert parallele Requests: genau einer gewinnt, alle anderen und
jeder spätere Versuch erhalten 409. Danach ist die Selbstregistrierung aus
(`OLLAMAIL_AUTH_LOCAL_REGISTRATION`); Admins legen Konten über `POST /api/users` an oder laden
per Link ein (`POST /api/users/invitations`). Notfallzugang: `python -m app.cli reset-password`
bzw. `create-admin`.

**Admin-Verwaltung** (#33, Details: [`auth/admin.md`](auth/admin.md)): Provider-Assistent mit
Redirect-URI und Verbindungstest, lokale Anmeldung abschaltbar (`auth_policy`), zentrale
Gruppen→Rollen-Zuordnung (`auth_role_mapping_rules`, ausgewertet in `provision_user` bei jedem
externen Login), Nutzerverwaltung (Rolle, Deaktivierung, Sitzungen, Einladungen in
`auth_invitations`). `app/auth/admin_access.py` erzwingt serverseitig, dass mindestens ein
aktiver Admin einen funktionierenden Zugang behält (409 `admin-lockout`).

**SCIM 2.0** (#95, `app/scim/`, Anleitung: [`auth/scim.md`](auth/scim.md)): `/api/scim/v2`
mit `Users`, `Groups`, `ServiceProviderConfig`, `Schemas`, `ResourceTypes` (RFC 7643/7644,
Filter `eq`/`and` und PATCH-Varianten von Entra ID und Okta). Bearer-Tokens aus dem Admin-Bereich
(`scim_tokens`, nur SHA-256, widerrufbar, Rate-Limit je Token und je IP), Schalter und
Linking-Anbieter in `scim_config`. Provisionierte Nutzer: `scim_users` (`userName`,
`externalId`; SCIM-ID = Nutzer-ID) plus eine Identität `scim`, in deren `groups` die SCIM-Gruppen
(`scim_groups`, `scim_group_members`; Name und `externalId`) gespiegelt werden – so wirken sie im
Rollen-Mapping (#33, auch beim Login) und bei Gruppen-Zuweisungen geteilter Postfächer (#34)
ohne Sonderfall. `active=false` deaktiviert und löscht alle Sessions in einer Transaktion,
`DELETE` nutzt `app.privacy.deletion.delete_user` (#36); beides mit Lockout-Schutz. Die
SCIM-Pfade sind vom CSRF-Schutz ausgenommen und nicht im OpenAPI-Dokument.

**Login** (`POST /api/auth/login`): Zuerst zählen zwei Fixed-Window-Zähler in Postgres
(atomares Upsert, vor der Passwortprüfung committet): pro Client-IP (`OLLAMAIL_AUTH_IP_MAX_ATTEMPTS`)
und pro Konto (`OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS` je `OLLAMAIL_AUTH_LOGIN_WINDOW_MINUTES`). Darüber
gibt es 429 (`retry_after` in Sekunden), auch bei richtigem Passwort; ein erfolgreicher Login setzt
den Kontozähler zurück. Unbekannte Konten werden genauso gezählt und mit einem Dummy-Hash geprüft,
damit Antwort und Laufzeit nichts verraten. Die Schlüssel sind HMACs von IP bzw. E-Mail-Adresse.
Argon2id (RFC 9106, 64 MiB) läuft in einem Thread, höchstens vier Hashes gleichzeitig; veraltete
Parameter werden beim Login aktualisiert. Hinter einem Reverse Proxy kommt die Client-IP aus
`X-Forwarded-For` (#137): Caddy ermittelt sie strikt von rechts (`trusted_proxies_strict`, nur
private Netze sind Proxys) und gibt genau einen Wert weiter; die API übernimmt ihn per
`ProxyHeadersMiddleware` nur von `OLLAMAIL_FORWARDED_ALLOW_IPS` (uvicorn läuft mit
`--no-proxy-headers`). Ein vom Client gefälschter Header erzeugt so keinen neuen IP-Zähler.

**Zweiter Faktor** (#96, `app/auth/mfa/`, Details: [`auth/mfa.md`](auth/mfa.md)): Lokale Konten
können Passkeys (WebAuthn mit `webauthn`, auch ohne Passwort), eine Authenticator-App (TOTP mit
`pyotp`, QR-Code lokal per `segno`) und einmal nutzbare Wiederherstellungscodes (nur als HMAC
gespeichert) einrichten. Hat ein Konto einen Faktor, antwortet `POST /api/auth/login` nach dem
Passwort mit 202 und **ohne Session**; der Zwischenzustand ist ein kurzlebiges, einmal nutzbares
Cookie (`ollamail_mfa`, Tabelle `auth_mfa_pending`), das an genau diesen Login gebunden ist. Erst
`/api/auth/mfa/verify*` startet die Session. Für den zweiten Schritt gelten IP-Limit, eine
eigene Kontosperre und höchstens 5 Versuche je Zwischenzustand. Admins können 2FA für Admins oder
alle lokalen Konten erzwingen (`auth_policy.mfa_enforcement`); dann wird der Faktor vor der ersten
Session eingerichtet – auch nach einer Einladung oder Selbstregistrierung, die denselben
Schrittfluss wie der Login nutzen. RP-ID und Origins kommen aus der Konfiguration
(`OLLAMAIL_AUTH_WEBAUTHN_*`, sonst `OLLAMAIL_AUTH_PUBLIC_URL`).

**Sessions:** Cookie `ollamail_session` (`HttpOnly`, `Secure`, `SameSite=Lax`, 256 Bit Zufall);
in der DB steht nur der SHA-256. Gültig bis `expires_at` (Lebensdauer) und solange die letzte
Anfrage weniger als das Idle-Timeout zurückliegt (`last_seen_at`, höchstens minütlich
geschrieben). `authenticated_at` hält fest, wann sich der Nutzer in dieser Session zuletzt
ausgewiesen hat (Login oder Bestätigung); sensible Endpunkte (Faktor entfernen, neue
Wiederherstellungscodes, Datenexport, Konto löschen; kritische Admin-Aktionen wie Nutzer
löschen, Rollen, SCIM-Tokens und -Einstellungen, Rollen-Zuordnung, Anmelde- und KI-Provider, KI-Cloud-Freigabe und Aufgaben-Zuordnung, Shared-Mailbox-Zuweisungen über `RecentAdminDep` bzw. `check_recent`, #190, #206) verlangen über `RecentAuthDep`
(`app/auth/reauth.py`) eine Bestätigung innerhalb von `OLLAMAIL_AUTH_REAUTH_MINUTES` per
Passwort, TOTP, Passkey oder erneuter (SSO-)Anmeldung, sonst 403 `reauth-required`
(Details: [`auth/mfa.md`](auth/mfa.md#bestätigung-vor-sensiblen-aktionen-144)). Jede Anfrage prüft Rolle und `is_active` neu; deaktivierte Nutzer verlieren sofort
den Zugriff. Login ersetzt eine vorhandene Session (keine Session Fixation). Endpunkte:
`GET /api/auth/me`, `PATCH /api/auth/me` (Name, Sprache, Zeitzone), `POST /api/auth/logout`,
`GET /api/auth/sessions`, `DELETE /api/auth/sessions/{id}`, `DELETE /api/auth/sessions`
(alle anderen; mit `?include_current=true` alle). Der Worker-Job `auth.cleanup` löscht stündlich
abgelaufene Sessions und Zähler. Die Sitzungsliste nennt je Sitzung das Anmeldeverfahren
(`provider` plus `provider_name`, der Anzeigename des konfigurierten Providers; `null` für lokal
und nicht mehr konfigurierte Provider, #208).

**Hinweis auf verknüpfte Anmeldungen (#208, `app/auth/link_notices.py`):** Verknüpft
`provision_user` eine externe Identität per E-Mail-Adresse mit einem bestehenden Konto
(`link_by_email` oder SCIM-Linking), entsteht neben `user.identity_linked` im Audit-Log eine Zeile
in `auth_identity_link_notices` (nur Nutzer-ID, Provider-Key, Zeitpunkt; `ON DELETE CASCADE`).
`GET /api/auth/link-notices` liefert die offenen Hinweise, `DELETE /api/auth/link-notices/{id}`
bestätigt einen. Beides gilt nur für Sitzungen eines *anderen* Anmeldeverfahrens als des
verknüpften Providers (fremde oder unsichtbare Hinweise: 404) – wer sich über die neue Verknüpfung
anmeldet, kann den Hinweis also weder sehen noch wegklicken. Die UI zeigt ihn als Hinweisleiste
(`SystemNotices`) mit Link zu Einstellungen → Sitzungen, wo sich die neue Sitzung beenden lässt.
Eine Verknüpfung selbst zu trennen ist bewusst nicht vorgesehen: Mit `link_by_email` würde der
Provider beim nächsten Login einfach neu verknüpfen; dafür ist der Admin zuständig.

**CSRF:** Signiertes Double-Submit-Cookie (`CSRFMiddleware`, gilt für die ganze App). Jede
Anfrage außer `GET`/`HEAD`/`OPTIONS`/`TRACE` muss den Wert des Cookies `ollamail_csrf` im Header
`X-CSRF-Token` senden. Der Token ist `<nonce>.<HMAC(nonce, Session-Cookie)>`: an die Session
gebunden, bei Login/Logout neu ausgestellt und von einer Subdomain aus nicht fälschbar. Fehlt das
Cookie oder passt es nicht zur Session, setzt jede Antwort ein neues. `Sec-Fetch-Site: cross-site`
wird zusätzlich abgewiesen. Abgewiesene Anfragen bekommen `403` mit `error_code: "csrf_failed"`;
Setup- und Anmeldeseite erklären damit den häufigsten Fall, ein über `http://` verworfenes
`Secure`-Cookie ([`OPERATIONS.md` 2.6](OPERATIONS.md#26-http-ohne-tls-testbetrieb)).

**Dependencies:** `get_current_session` (401), `require_admin` (403), `get_current_user` (ORM-Objekt)
in `app/auth/dependencies.py`; `get_current_user_id` in `app/core/current_user.py`. Die DB-Session
der Auth-Prüfung ist nach der Prüfung wieder frei (`Depends(get_db, scope="function")`), damit
SSE-Streams keine Pool-Verbindung halten.

### Shared Mailboxes

Ein Postfach gehört entweder einem Nutzer oder ist ein **Shared Mailbox**, das vom Admin angelegt und
Nutzern/Gruppen zugewiesen wird. Zugriffsrechte gelten für alle Features (Triage, Todos, RAG, Digest).

**Umsetzung (#34):**

- **Zuweisungen** (`mail_mailbox_assignments`, `app/mail/models.py`): je Zeile genau ein Nutzer
  (`user_id`) oder eine Gruppe (`group_name`, optional `provider`), Recht `read` oder `act`
  („Mails verwalten“: zusätzlich gelesen/ungelesen, markieren, archivieren, verschieben,
  Papierkorb; #148). Senden aus Shared Mailboxes gibt es nicht (`send` nur für Besitzer). Gruppen sind die, die eine Identität des Nutzers beim letzten Login gemeldet hat
  (`auth_identities.groups`: OIDC-Gruppen-Claim, LDAP-Gruppen-DNs, GitHub-Teams), verglichen ohne
  Groß-/Kleinschreibung wie beim Rollen-Mapping (#33). Änderungen der Gruppenmitgliedschaft im
  Verzeichnis wirken mit dem nächsten Login.
- **Eine Zugriffsregel:** `accessible_mailbox_ids(user_id)` in `app/mail/access.py` ist eine
  SQL-Unterabfrage (eigene Postfächer ∪ zugewiesene Shared Mailboxes). Alle Abfragen nutzen sie:
  Postfach- und Mail-API (Inbox, Thread, Body, Anhänge), Triage, Todos, Suche, RAG (Abruf,
  gespeicherte Zitate *und* Antworten), Digest (Erzeugung und gespeicherte Digests, Podcast-Feed).
  Es gibt keinen Cache: Ein Entzug wirkt mit der nächsten Anfrage. Gespeicherte RAG-Antworten,
  die ein nicht mehr lesbares Postfach zitieren, werden ohne Text ausgeliefert (`withheld`) und
  nicht als Verlauf an das Modell gegeben; Digests mit einem nicht mehr lesbaren Postfach sind
  nicht mehr abrufbar. Getestet für jedes Feature in `backend/tests/shared/test_access.py`.
- **Admin-API** (`/admin/shared-mailboxes`, nur Admins, `app/mail/api/shared.py`): anlegen
  (Verbindungstest, Zugangsdaten verschlüsselt), umbenennen, Zugangsdaten/Sync-Einstellungen,
  pausieren, Ordner, Sync anstoßen, entfernen (im Hintergrund wie oben), Zuweisungen ersetzen
  (`PUT …/assignments`, `{"users": [...], "act_users": [...], "groups": [{"group", "provider",
  "permission"}]}`; `act_users` erhalten `act`). Antworten enthalten
  nur Metadaten (Status, Anzahlen, Zuweisungen, `reader_count`), nie Mails. Admins lesen ein
  Shared Mailbox nur, wenn sie sich zuweisen – sichtbar im Audit-Log.
- **Audit:** `mailbox.shared` (mit `permission`, auch wenn sich nur das Recht ändert) und
  `mailbox.unshared` je Nutzer bzw. Gruppe (Nutzer-ID bzw. Gruppenname, falls kurz und ohne `@`,
  sonst nur die Zuweisungs-ID), in derselben Transaktion.
- **Einmal synchronisiert:** Ein Shared Mailbox ist eine Zeile in `mail_mailboxes`; Sync-Job,
  Mails, Verarbeitung (Triage, Todos, Suchindex) gibt es genau einmal, egal wie viele es lesen.
  Events (`mailbox.sync`, `mailbox.changed`, `message.processed`) gehen an alle aktuellen Leser
  (`app.mail.access.publish_to_readers`); wer Zugriff erhält oder verliert, bekommt
  `mailbox.changed` (`assigned`/`revoked`).
- **Nur lesen bzw. Mails verwalten:** Nutzer eines Shared Mailbox dürfen weder Einstellungen
  ändern noch synchronisieren noch senden. Gelesen/ungelesen, Markierungen und Ordner gehören dem
  Postfach; ändern dürfen sie nur Nutzer mit `act`-Zuweisung. Triage-Korrekturen sind erlaubt
  und wirken postfachweit (§4.2).
- **Löschen:** Wird ein Nutzer gelöscht, verschwinden nur seine Zuweisungen (`ON DELETE
  CASCADE`) und seine Team-Todo-Zuweisungen (`SET NULL`); das Shared Mailbox bleibt.
- **UI:** Admin → Geteilte Postfächer (anlegen mit dem IMAP-Formular, Personen und Gruppen
  zuweisen); Shared Mailboxes stehen in der Navigation in einem eigenen Abschnitt und öffnen die
  Inbox gefiltert auf das Postfach; unter Einstellungen → Postfächer erscheinen sie getrennt und
  ohne Aktionen.

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
- i18n (DE/EN, jede Sprache ein eigener Chunk, geladen wird nur die aktive), Dark/Light/System, PWA,
  Tastaturbedienung und Command Palette (⌘K).
- Gestaltung: siehe `docs/DESIGN.md`.

## 7. Betrieb

- Konfiguration per Env (`OLLAMAIL_*`), dokumentiert in `deploy/.env.example`. Start mit Docker Compose: `deploy/README.md`.
- Kubernetes: Helm-Chart `deploy/helm/ollamail`, Doku in [`operations/kubernetes.md`](operations/kubernetes.md).
- Health-Endpunkte `/healthz` (live) und `/readyz` (DB, Queue, LLM erreichbar); der Worker
  meldet Liveness über eine Heartbeat-Datei (`app/core/heartbeat.py`).
- Strukturierte JSON-Logs ohne personenbezogene Inhalte; optional Prometheus-Metriken
  (`OLLAMAIL_METRICS_ENABLED`, nur intern bzw. mit Token; nur IDs, Codes und Zähler,
  `app/core/metrics.py`, `app/admin/metrics.py`).
- Datenbank: ein gemeinsamer Verbindungspool je Prozess (`app.core.db.process_database`);
  Verbindungsbudget und `max_connections` in [`OPERATIONS.md` §8.2](OPERATIONS.md#82-datenbankverbindungen).
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
