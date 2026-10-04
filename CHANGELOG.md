# Changelog

Alle nennenswerten Änderungen an ollamail. Die Versionsnummern folgen
[Semantic Versioning](https://semver.org/lang/de/); vor 1.0 können sich Konfiguration und API
noch ändern. Die vollständige Liste je Version erzeugt `git-cliff` aus den Commits
(`.github/cliff.toml`); sie steht auch in den Release-Notes auf GitHub.

## [0.1.0] – Entwurf

Erste Version. Der Tag wird vom Repository-Owner gesetzt; bis dahin ist dieser Abschnitt ein
Entwurf.

### Hauptfunktionen

- **Postfächer:** IMAP (mit IDLE und Autodiscovery), Microsoft 365 (Graph) und Gmail /
  Google Workspace; geteilte Postfächer mit serverseitiger Zugriffsprüfung. Antworten und
  Antwortentwürfe werden über SMTP, Graph bzw. die Gmail-API gesendet.
- **Mail-Aktionen:** Archivieren (`e`), Verschieben (`v`), In den Papierkorb (`#`) und Markieren
  (`s`) über IMAP, Graph und Gmail, sofort mit „Rückgängig“; in geteilten Postfächern nur mit dem
  Recht „Mails verwalten“, jede Aktion im Audit-Log.
- **Triage:** Kategorien und Priorität je Mail mit Begründung, Vorfilter für Newsletter und
  Benachrichtigungen, Absenderregeln, lernt aus Korrekturen; optionales Zurückschreiben als
  Label/Ordner.
- **Benachrichtigungen:** Browser-Notification bei neuen Mails in gewählten Kategorien (z. B.
  „Wichtig“, „Handlungsbedarf“), nur nach Opt-in, standardmäßig ohne Betreff und lautlos; solange
  ollamail in einem Tab geöffnet ist. Optional per Web Push auch ohne geöffneten Tab (vom Admin
  freizuschalten, je Gerät einzuschalten; läuft über den Push-Dienst des Browser-Herstellers).
- **Aufgaben:** automatisch aus Mails extrahiert, manuell ergänzbar; Export nach CalDAV,
  Microsoft To Do und Google Tasks (vom Admin freizuschalten).
- **Suche und „Frag deine Inbox“:** Hybrid-Suche (Volltext + Embeddings) über Mails und Anhänge,
  OCR für gescannte PDFs, Antworten mit Quellenangaben.
- **Daily Digest:** Zusammenfassung der letzten Mails als Text und Audio (Piper, Deutsch und
  Englisch), Web-Player und privater Podcast-Feed.
- **KI lokal:** Provider-Abstraktion für Ollama und OpenAI-kompatible Server, Hardware-Profile
  (CPU, Consumer-GPU, Server-GPU), Modellwahl je Aufgabe im Admin-Bereich; Cloud-LLMs nur per
  Admin-Opt-in.
- **Anmeldung:** Erst-Admin per Setup-Token, lokale Konten mit Passkeys/TOTP, OIDC (Entra ID,
  Google, generisch), GitHub, LDAP/Active Directory, SAML 2.0, SCIM-Provisionierung,
  Gruppen-Rollen-Mapping.
- **Datenschutz:** verschlüsselte Zugangsdaten mit Key-Rotation, PII-Filter im Logging,
  manipulationssicheres Audit-Log, Aufbewahrungsfristen, Datenexport und Kontolöschung.
- **Betrieb:** Docker Compose (Multi-Arch-Images amd64/arm64 über GHCR oder lokaler Build),
  Helm-Chart, Backup/Restore- und Upgrade-Anleitung in `docs/OPERATIONS.md`. Worker-Healthcheck
  per Heartbeat, optionale Prometheus-Metriken (`OLLAMAIL_METRICS_ENABLED`, nur intern bzw. mit
  Token), ein Datenbank-Pool je Prozess und PostgreSQL-Tuning für pgvector (#146).
- **Oberfläche:** schlichte UI in Deutsch und Englisch, Light/Dark, Desktop und Mobil,
  Befehlsmenü und Tastaturkürzel.

### Bekannte Einschränkungen

- ollamail ist ein Analyse-Werkzeug, kein Mail-Client: Auf den Server zurück gehen nur
  gelesen/ungelesen, Markieren, Archivieren, Verschieben, In den Papierkorb, gesendete Antworten
  und – falls eingeschaltet – die Triage-Kategorie als Label/Ordner. Keine neuen Mails, keine
  Ordnerverwaltung, kein endgültiges Löschen (`docs/ARCHITECTURE.md` §3.1, „Mail-Aktionen auf
  dem Server“).
- In IMAP-Postfächern bleiben Mails, die in den Papierkorb verschoben wurden, in ollamail
  gespeichert (der Papierkorb wird standardmäßig nicht synchronisiert); bei Microsoft 365 und
  Gmail entfernt sie der nächste Sync. „Rückgängig“ wirkt nur bis dahin.
- Microsoft 365, Gmail/Google Workspace, Microsoft To Do und Google Tasks sind nur mit
  Unit- und Integrationstests gegen nachgebaute APIs geprüft, **nicht gegen echte Konten**.
  Testanleitungen: `docs/providers/microsoft365.md`, `docs/providers/gmail.md`.
- SAML, SCIM und die externen Identity-Provider sind ebenfalls nicht gegen echte
  Entra-ID-/Okta-/Google-Mandanten getestet.
- Die arm64-Images werden nativ auf arm64-Runnern gebaut (seit #131), aber auf keiner echten
  arm64-Hardware getestet.
- Die GHCR-Images sind derzeit nicht öffentlich; ohne Zugriff lokal bauen
  (`deploy/compose.build.yaml`, siehe `docs/OPERATIONS.md` §2.3).
- Kein automatischer Downgrade: Rückkehr zu einer älteren Version nur über das Backup von
  vor dem Update (`docs/OPERATIONS.md` §6.4).
- Paperless-ngx-Anbindung ist bewusst zurückgestellt (`docs/ROADMAP.md`).
- Qualität von Triage, Aufgaben und Digest hängt stark vom gewählten Modell ab; auf CPU-only
  Hosts sind kleine Modelle langsam (Richtwerte in `docs/OPERATIONS.md` §3).
- TTS nur Deutsch und Englisch (Piper-Standardstimmen); die Stimmen werden beim ersten Bedarf
  von huggingface.co geladen oder müssen offline ins Volume kopiert werden.

### Alle Änderungen

<details>
<summary>Aus den Commits erzeugt (git-cliff, Stand dieses Entwurfs)</summary>

#### Features

- **frontend:** Scaffold React app with Vite, Tailwind v4, shadcn/ui and TanStack (afe15ab)
- **deploy:** Add Dockerfiles and Docker Compose stack (bea8e87)
- **core:** Add config, async db, alembic, health probes, logging with PII filter (7d19a61)
- **deploy:** Always run migrations on start now that Alembic exists (39e0b7b)
- **frontend:** Add design system, app shell, command palette and shortcuts (00d69dc)
- **mail:** Add mail data model, provider interface and MIME normalisation (2907da3)
- **deploy:** Use GHCR images by default with local build override (f932630)
- **worker:** Add Procrastinate job queue and LISTEN/NOTIFY event stream (33a75b3)
- **ai:** Add LLM provider abstraction with Ollama and OpenAI-compatible backends (8e5b1e1)
- **core:** Add envelope encryption for stored secrets with key rotation (e8f54db)
- **api:** Export OpenAPI schema without a server and use stable operation IDs (b6de8b3)
- **frontend:** Generate typed API client with central error handling and SSE hook (cc62f33)
- **mail:** Add IMAP provider with own asyncio client (73caf35)
- **mail:** Add mailbox sync engine, sync job, IDLE watcher and message_stored hook (a810cf4)
- **auth:** Add users, sessions, CSRF, first-admin setup and local login (29390de)
- **processing:** Add message processing pipeline orchestration (8443747)
- **frontend:** Polish app shell after screenshot review (647cac9)
- **mail:** Queue processing for stored messages, backfill for the initial import (cd16172)
- **tts:** Add TTS engine abstraction with Piper, text normalization and encoding (724ba4c)
- **processing:** Add optional step predecessors (after) (910fac3)
- **todos:** Extract todos from mails and add todo API (5493f9a)
- **mail:** Support mailbox-wide sync cursors in the sync engine (45e2851)
- **mail:** Add Gmail / Google Workspace provider (31d4cdf)
- **mail:** Add Gmail OAuth connect endpoints and docs (cd19efe)
- **frontend:** Add setup wizard, login, route guards and account settings (49201c9)
- **audit:** Add append-only, hash-chained audit log (99811f6)
- **admin:** Add audit log page with filters and CSV export (49cb519)
- **search:** Add hybrid search index with chunking, attachment text and pgvector (a9f8692)
- **auth:** Add LDAP / Active Directory sign-in (dba4630)
- **mail:** Add mailbox API (add, test, folders, sync status, remove) (db3a960)
- **mail:** Record mailbox creation and removal in the audit log (07b6534)
- **auth:** Add OIDC login with presets and just-in-time provisioning (a4b02bb)
- **triage:** Add triage step with prefilter, LLM classification and feedback (4b74ee5)
- **triage:** Provide the triage category to the todo step (9cbe781)
- **auth:** Add group-to-role mapping, user administration and admin lockout protection (68b9e22)
- **admin:** Add sign-in, role mapping and user administration UI (401d11a)
- **admin:** Show provider names in the user list and tidy the rule editor (c9296c7)
- **auth:** Add GitHub login with organization and team restriction (31d4092)
- **mail:** Add MessageChanged event and credential saving to the sync engine (f7427b8)
- **mail:** Add Microsoft 365 provider via Microsoft Graph (fb2fbd4)
- **mail:** Record connected Microsoft 365 mailboxes in the audit log (8a90838)
- **search:** Filter search results by triage category (c2a46ad)
- **rag:** Answer inbox questions with streamed, cited answers (9a3623a)
- **admin:** Offer GitHub in the provider wizard and guard GitHub changes against lockout (e531f8e)
- **digest:** Add daily digest with audio and private podcast feed (adb06f0)
- **ai:** Store AI settings in the database with admin API (f3da0b3)
- **admin:** Add AI settings page and cloud notice (8ed9609)
- **mail:** Add read API for messages, threads and attachments (4e01a8a)
- **inbox:** Add mailbox management and inbox with thread view (f7cb6a5)
- **mail:** Offer Microsoft 365 connect and merge migration heads (09b87bb)
- **mail:** Add shared mailboxes with central access check (69a257a)
- **privacy:** Add retention job, data export and account deletion (402ab56)
- **frontend:** Add data export, account deletion and retention admin (2c743fc)
- **frontend:** Manage and show shared mailboxes (a69e257)
- **triage:** Add batch triage, inbox ordered by category and live event (b0e93a4)
- **frontend:** Add triage UI with grouped inbox and category settings (0767a23)
- **digest:** List selectable voices for the digest settings (896ef9a)
- **digest:** Add digest page with player, transcript, archive and settings (fe1e6aa)
- **search:** Add classic search endpoint with excerpts (e1afd23)
- **search:** Add search and answer UI with sources and history (1e3c32a)
- **todos:** Filter todo list by source message (9701084)
- **tasks:** Task list with groups, inline editing and mail tasks (a2d430e)
- **tasks:** Align task columns and stack date and mail on phones (5311af2)
- **tasks:** Find task commands when searching for tasks (5cdbbc8)
- **admin:** Delete users from the user list (d856cae)
- **mail:** Send replies through the MailProvider interface (c486bf5)
- **drafts:** Generate, edit and send reply drafts (9d734e8)
- **deploy:** Add Helm chart for Kubernetes (cb90f6f)
- **scim:** Add SCIM 2.0 provisioning for Entra ID and Okta (ac10d69)
- **drafts:** Reply editor in the thread and drafts overview (7c9963b)
- **auth:** Add SAML 2.0 login (SP-initiated) (6f00d3c)
- **admin:** Add SAML to the identity provider wizard (8f38aa9)
- **todos:** Export todos to CalDAV (VTODO) via a TodoSink interface (63ec694)
- **tasks:** Task export settings and export state in the task list (ad2ae68)
- **auth:** Add passkeys, TOTP and recovery codes for local accounts (86261dc)
- **frontend:** Add security settings and second-factor sign-in steps (0364020)
- **privacy:** Include second factors in the personal data export (4e34148)
- **search:** OCR scanned attachments with Tesseract on an ocr queue (f02616b)
- **search:** Mark hits and citations from OCR text in the UI (dd7ea3e)
- **todos:** Export todos to Microsoft To Do (#120) (29a1580)
- **todos:** Export todos to Google Tasks (#121) (fac37a5)

#### Bug Fixes

- **admin:** Let the event filter fill the row on narrow screens (23f71f6)
- **web:** Start redirect logins via the provider's login_path (91605e3)
- **admin:** Do not preselect copyable values when a sheet opens (80b1edb)
- **db:** Base the idp admin migration on the merged main head (52c1244)
- **mail:** Rebase shared mailbox migration onto the GDPR head (116fe6f)
- **frontend:** Highlight only the open shared mailbox and compact the read-only hint (1963ee1)
- **web:** Type the login error helper with TFunction (42b2d83)
- **search:** Keep chips usable by keyboard and refine highlighting (c2d377b)
- **frontend:** Type login error helper with TFunction (9ee5665)
- **triage:** Stop refetching failed categories in a loop (b75a3df)
- **tests:** Keep the periodic deferrer out of test workers (3ef8250)
- **deploy:** Keep the helm test pod for helm test --logs (9dfa37c)
- **drafts:** Clearer recipient toggle and overview preview (4798888)
- **tests:** Keep the periodic deferrer out of test workers (b2116d7)
- **auth:** Answer wrong setup codes with 400 instead of 401 (98a32f2)
- **frontend:** Give the 2FA enforcement select its full width (fb741ec)
- **deploy:** Let the Helm chart consume the ocr queue (f2b6745)
- **tasks:** Keep task updates off the export settings query (ad4508c)
- **tasks:** Keep task updates off the export settings query (9c25f1d)
- **mail:** Keep server fields the user is in or edited from late autodiscovery (#119) (930226d)
- **inbox:** Keep palette commands stable while pages load (#118) (182bc7b)

#### Performance

- **inbox:** Cut main-thread work while scrolling the message list (63d8273)

#### Documentation

- Add project foundation docs, contribution rules and license (b3f341d)
- Document unenforced branch protection and add pre-push guard for main (716fb7e)
- **architecture:** Reference Compose profiles and deploy README (6addbf2)
- **privacy:** Describe how the logging filter enforces the no-PII rule (0735e55)
- Require screenshots for UI changes in pull requests (fc750e9)
- **ops:** Add operations guide for self-hosting admins (db3499a)
- **mail:** Document IMAP provider, sync and new mail settings (82a9e4a)
- **mail:** Add Gmail provider design document (1bcee7d)
- **audit:** Document audit log events, guarantees and retention setting (8932500)
- **auth:** Document LDAP / Active Directory sign-in (83601ba)
- **auth:** Document OIDC sign-in for Entra ID, Google, Keycloak and Authentik (fcd8cc5)
- **auth:** Document sign-in administration, role mapping and emergency access (3c27e3d)
- **auth:** Document GitHub login (d2bec11)
- **mail:** Document the Microsoft 365 provider (d744374)
- **rag:** Document ask-your-inbox pipeline and deletion (95af039)
- **digest:** Document digest settings, feed and retention (b4f5359)
- Describe shared mailboxes and the central access rule (f5bf1c6)
- **digest:** Document the digest UI and the voices endpoint (8f771b6)
- **frontend:** Describe triage categories error state (a848709)
- **drafts:** Document reply drafts, sending and provider permissions (c1ad6f5)
- **auth:** Document SAML login (5ec4a27)
- **todos:** Document the todo export (CalDAV) and its privacy measures (8925f24)
- Describe E2E suite and Compose smoke test (11e03aa)
- **auth:** Document two-factor authentication for local accounts (edc527d)
- **search:** Document OCR settings, queue, throughput and image size (9c1a86f)
- Reflect that the main ruleset is enforced now that the repo is public (#117) (98d6994)

#### Tests

- **mail:** Run IMAP integration tests against Dovecot in CI (54aaca9)
- **search:** Isolate tests from index state committed by other workers (81051f7)
- **triage:** Make sender rule listing test collation-independent (2a58c49)
- **auth:** Cover the lockout guard for GitHub providers (b672c9f)
- **inbox:** Add e2e tests for inbox and mailbox setup, document mail views (3a7eb31)
- **tasks:** E2e for checking off, due dates and jumping to the mail (a983daa)
- **mail:** Compare folder and UID of the sent copy, not UIDVALIDITY (3b3c451)
- **auth:** Run SAML login against Keycloak in CI (a1c82e8)
- **e2e:** Fix stale mailbox mock and racy heading locators (eae0c37)
- **e2e:** Add full-stack specs for tasks and search, run without retries (fa8b14b)
- **e2e:** Wait for indexing via the API and for autodiscovery before editing (502dc8a)
- **e2e:** Make the frame-time budget of the scroll test configurable (fc61260)
- **auth:** Deactivated accounts cannot finish a second-factor sign-in (8a89e89)

#### CI / Build

- Add GitHub Actions workflow for backend and frontend (0211cc3)
- Fetch full history for paths-filter on push to main (8fa857f)
- **release:** Build multi-arch images and publish them to GHCR (377a63b)
- Fail when the generated API client is out of date (691789b)
- Install slapd for LDAP integration tests (d24cda3)
- Add E2E job and Compose smoke test (6b2ad80)
- Cut runner minutes for releases, compose and helm runs (#116) (3cfaa17)

#### Chores

- **backend:** Scaffold FastAPI project with uv, ruff, mypy and pytest (144e408)
- **api:** Regenerate API client for GET /events (25e6dc1)
- **api:** Regenerate API client for /todos (70f1d20)
- **api:** Regenerate API client for /mail/graph/connect (3f896fe)
- **api:** Regenerate API client for /rag endpoints (0813afd)
- **api:** Regenerate API client for /digests (500bc1c)

#### Style

- **auth:** Format LDAP client tests (ea090a6)
- **admin:** Full-width selects in the provider form (391b328)


</details>

[0.1.0]: https://github.com/dusseligerdussel/ollamail/releases/tag/v0.1.0
