# Microsoft 365 (Exchange Online) über Microsoft Graph

Design und Betriebsanleitung für den Mail-Provider `graph` (Issue #37). Code:
`backend/app/mail/providers/graph*.py`. Der Provider implementiert das allgemeine
`MailProvider`-Interface (`docs/ARCHITECTURE.md` §3.1) und nutzt die vorhandene Sync-Engine,
den Watcher und die Normalisierung. IMAP mit Basic-Auth wird für Exchange Online nicht
gebraucht.

## 1. Ziele und Nicht-Ziele

- Eigene Postfächer per OAuth (delegiert) und Shared Mailboxes bzw. org-weite Rollouts per
  App-only-Berechtigung anbinden.
- Delta-Sync pro Ordner, Kategorien als Labels, verschieben und Flags setzen.
- **Self-Hosting ohne öffentliche URL ist der Normalfall:** Polling ist Standard. Change
  Notifications (Webhooks) sind optional und nur mit öffentlich erreichbarer URL möglich.
- Tokens verschlüsselt (`mail_mailboxes.credentials`, `EncryptedJSON`), automatischer Refresh.
- Antworten senden (`createReply` + `send`, #92, siehe Abschnitt 4).
- Nicht in diesem Schritt: Frontend (folgt mit der Postfach-UI), Admin-UI für die Entra-App
  (vorerst nur Umgebungsvariablen).

## 2. App-Registrierung in Entra ID

Eine App-Registrierung genügt für alle Postfächer einer Instanz. Die App aus dem OIDC-Login
(#30, `docs/auth/oidc.md`) kann wiederverwendet werden: dort zusätzlich die Redirect-URI und
die Graph-Berechtigungen unten eintragen und dieselbe Client-ID/dasselbe Secret in
`OLLAMAIL_MAIL_GRAPH_*` setzen. Eine eigene App ist sauberer, wenn Login und Postfachzugriff
getrennt freigegeben werden sollen.

1. Entra Admin Center → *App registrations* → *New registration*.
   - *Supported account types*: „Accounts in this organizational directory only“ (Single
     Tenant) oder „Any organizational directory“ (Multi-Tenant).
   - *Redirect URI* (Plattform **Web**): `https://<ollamail-host>/api/mail/graph/callback`.
2. *Certificates & secrets* → *New client secret*. Wert in `OLLAMAIL_MAIL_GRAPH_CLIENT_SECRET`.
   Ablaufdatum notieren; ein abgelaufenes Secret führt zu `authentication_failed`.
3. *API permissions* → *Microsoft Graph*:
   - **Delegiert** (Nutzer verbindet sein Postfach): `Mail.ReadWrite`, `Mail.Send`, `User.Read`,
     `offline_access`; für freigegebene Postfächer zusätzlich `Mail.ReadWrite.Shared` und
     `Mail.Send.Shared`.
   - **Application** (App-only, Shared Mailboxes/Rollout): `Mail.ReadWrite`, zum Senden
     `Mail.Send`, danach *Grant admin consent*.
4. Umgebungsvariablen (siehe `deploy/.env.example`):

| Variable | Bedeutung |
|---|---|
| `OLLAMAIL_MAIL_GRAPH_CLIENT_ID` | Application (client) ID. Ohne Wert ist der Provider aus. |
| `OLLAMAIL_MAIL_GRAPH_CLIENT_SECRET` | Client-Secret (nur in der Umgebung, nie im Repo). |
| `OLLAMAIL_MAIL_GRAPH_TENANT_ID` | Tenant-ID. Standard `organizations` (alle Arbeitskonten); für App-only ist eine konkrete Tenant-ID Pflicht. |
| `OLLAMAIL_MAIL_GRAPH_REDIRECT_URI` | Redirect-URI wie registriert. Leer: aus der Anfrage abgeleitet (`<schema>://<host>/api/mail/graph/callback`). |
| `OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL` | Öffentliche URL für Change Notifications. Leer (Standard): nur Polling. |
| `OLLAMAIL_MAIL_GRAPH_SEND_ENABLED` | `Mail.Send` beim Verbinden anfordern, damit Antworten gesendet werden können (Standard `true`, siehe Abschnitt 4). `false`: Postfächer werden nur mit Lese-/Schreibzugriff verbunden, Senden liefert `send_not_permitted`. |
| `OLLAMAIL_MAIL_GRAPH_AUTHORITY`, `OLLAMAIL_MAIL_GRAPH_API_URL` | Login- und Graph-Endpunkt (nationale Clouds, Tests). |
| `OLLAMAIL_MAIL_GRAPH_TIMEOUT`, `OLLAMAIL_MAIL_GRAPH_MAX_RETRIES` | Timeout je Anfrage, Wiederholungen bei Drosselung. |

Die Admin-UI für diese Werte (verschlüsselt in der DB) folgt mit der Admin-Oberfläche; bis
dahin gilt die Umgebung (GitOps-tauglich).

## 3. Berechtigungsmodelle

### 3.1 Delegiert (Standard für persönliche Postfächer)

Der Nutzer verbindet sein Postfach selbst (Authorization Code Flow mit PKCE und Client-Secret).
Die App sieht nur, was der Nutzer sieht; Admin-Consent ist nur nötig, wenn der Tenant
Nutzer-Consent abgeschaltet hat.

Ablauf (`backend/app/mail/providers/graph_router.py`):

1. `POST /api/mail/graph/connect` (angemeldet, CSRF-geschützt), optional
   `{"shared_mailbox": "team@contoso.com", "return_to": "/settings/mailboxes"}`. Antwort:
   `{"authorization_url": "https://login.microsoftonline.com/..."}`. `state`, PKCE-Verifier,
   Nutzer-ID und Rücksprungziel liegen in einem kurzlebigen (10 min), verschlüsselten und
   authentifizierten Cookie (AES-256-GCM, Schlüssel aus `OLLAMAIL_SECRET_KEY` abgeleitet) –
   serverseitig wird nichts gespeichert.
2. Das Frontend navigiert zu `authorization_url`; Microsoft leitet zurück auf
   `GET /api/mail/graph/callback?code=…&state=…`.
3. Callback: `state` muss zum Cookie passen und der angemeldete Nutzer zum Nutzer im Cookie
   (kein Login-CSRF, kein Unterschieben fremder Postfächer). Danach Code gegen Tokens tauschen,
   `GET /me` (bzw. `GET /users/{shared}` für ein freigegebenes Postfach) lesen und das
   Postfach anlegen oder – bei erneutem Verbinden – die Tokens ersetzen. Weiterleitung auf
   `return_to?graph=connected&mailbox_id=<id>` bzw. `?graph=error&reason=<code>`.
4. Der Watcher bemerkt das neue Postfach beim nächsten Abgleich (≤ 60 s) und stößt den
   Initialimport an.

Gespeichert wird (verschlüsselt) `{"refresh_token", "access_token", "expires_at"}`; in
`provider_settings` stehen nur nicht geheime Werte: `{"auth": "delegated", "user": "me" |
"<shared-address>", "tenant_id": "<tid>", "user_id": "<Graph-ID>"}`.

### 3.2 App-only mit Admin-Consent (Shared Mailboxes, org-weiter Rollout)

Client-Credentials-Flow, kein Nutzer beteiligt. **Ohne Einschränkung darf die App jedes
Postfach des Tenants lesen** – das ist für den Betrieb inakzeptabel. Pflicht ist deshalb eine
Einschränkung auf die freigegebenen Postfächer:

- **RBAC for Applications in Exchange Online** (empfohlen, löst `ApplicationAccessPolicy` ab):

  ```powershell
  Connect-ExchangeOnline
  New-ServicePrincipal -AppId <client-id> -ObjectId <enterprise-app-object-id> -DisplayName "ollamail"
  New-ManagementScope -Name "ollamail-mailboxes" -RecipientRestrictionFilter "MemberOfGroup -eq '<DN der Mail-Sicherheitsgruppe>'"
  New-ManagementRoleAssignment -App <client-id> -Role "Application Mail.ReadWrite" -CustomResourceScope "ollamail-mailboxes"
  Test-ServicePrincipalAuthorization -Identity <client-id> -Resource team@contoso.com
  ```

  Bei RBAC for Applications wird die Graph-Berechtigung `Mail.ReadWrite` (Application) **nicht**
  zusätzlich im Entra-Portal vergeben, sonst gilt sie tenantweit.
- **`ApplicationAccessPolicy`** (älter, weiter unterstützt): Graph-Berechtigung
  `Mail.ReadWrite` (Application) mit Admin-Consent, dann

  ```powershell
  New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId ollamail-mailboxes@contoso.com -AccessRight RestrictAccess -Description "ollamail"
  Test-ApplicationAccessPolicy -Identity team@contoso.com -AppId <client-id>
  ```

Ein App-only-Postfach wird vom Admin über die Postfach-API (#15) angelegt:
`type = "graph"`, `provider_settings = {"auth": "application", "user": "team@contoso.com"}`
(optional `"tenant_id"`, sonst `OLLAMAIL_MAIL_GRAPH_TENANT_ID`), keine `credentials`. Shared
Mailboxes (`is_shared`) legt der Admin unter `/api/admin/shared-mailboxes` an und weist sie
Nutzern und Gruppen zu (#34). Der Access-Token wird nur im
Speicher des Prozesses gehalten (≤ 1 h), nicht gespeichert. Verweigert die Policy den Zugriff,
meldet der Sync `access_denied`.

### 3.3 Freigegebene Postfächer mit delegiertem Zugriff

Hat ein Nutzer in Exchange *Full Access* auf ein Postfach, kann er es selbst verbinden
(`shared_mailbox` beim Connect, Scope `Mail.ReadWrite.Shared`). Das Postfach gehört dann dem
Nutzer (nicht `is_shared`); Zugriff endet, wenn Exchange die Berechtigung entzieht.

## 4. Senden von Antworten (#92)

Antworten aus `app/drafts` sendet `GraphProvider.send` mit zwei Aufrufen: `POST
/messages/{id}/createReply` (bzw. `createReplyAll`) auf der beantworteten Mail mit Empfängern,
Betreff und Text des Entwurfs, dann `POST /messages/{draft-id}/send`. Exchange setzt
`In-Reply-To`/`References`, hält die Antwort in der Konversation und legt sie in „Gesendete
Elemente“ ab. Schlägt `send` fehl, wird die erzeugte Antwort wieder gelöscht. Gesendet wird ohne
automatische Wiederholung (auch nicht nach Drosselung), damit keine Mail doppelt rausgeht.

**Berechtigungen:**

- **Delegiert:** Beim Verbinden werden zusätzlich `Mail.Send` (bzw. `Mail.Send.Shared` für ein
  freigegebenes Postfach) angefordert. Der Sync erneuert seine Tokens weiter mit den bisherigen
  Scopes; nur zum Senden holt der Provider ein Token mit `Mail.Send`. Postfächer, die **vor**
  dieser Version verbunden wurden, synchronisieren also unverändert, senden aber erst nach
  einem erneuten Verbinden (sonst `send_not_permitted`). Hat der Tenant Nutzer-Consent
  abgeschaltet, muss der Admin `Mail.Send` (delegiert) freigeben.
- **App-only:** Anwendungsberechtigung `Mail.Send` mit Admin-Consent. Wie `Mail.ReadWrite` gilt
  sie ohne Einschränkung für **jedes** Postfach des Tenants – mit RBAC for Applications die
  Rolle `Application Mail.Send` auf denselben Management Scope beschränken
  (`New-ManagementRoleAssignment -App <client-id> -Role "Application Mail.Send"
  -CustomResourceScope "ollamail-mailboxes"`) bzw. die `ApplicationAccessPolicy` gilt auch für
  `Mail.Send`. Shared Mailboxes sind in ollamail vorerst nur lesbar; Senden aus ihnen lehnt die
  API ab (403 `read_only`).
- `OLLAMAIL_MAIL_GRAPH_SEND_ENABLED=false` schaltet das Anfordern und das Senden ab.

## 5. Token-Handling (`graph_auth.py`)

- **Refresh:** Vor jeder Anfrage wird geprüft, ob der Access-Token noch mindestens 5 Minuten
  gilt; sonst Refresh (`grant_type=refresh_token`). Antwortet Graph mit 401, wird einmal
  erneuert und wiederholt.
- **Speichern:** Microsoft rotiert Refresh-Tokens. Neue Tokens schreibt der Provider über den
  provider-neutralen Rückruf `MailboxConfig.save_credentials` sofort in einer eigenen
  Transaktion – unabhängig vom Sync-Commit, damit ein abgebrochener Sync keinen frischen Token
  verliert. Sync-Job und Watcher können parallel erneuern; das ist unkritisch, weil ein alter
  Refresh-Token bis zu seinem Ablauf gültig bleibt.
- **Widerruf:** `invalid_grant` (Passwortänderung, Widerruf durch Admin, 90 Tage ungenutzt) →
  `AuthenticationError` (`authentication_failed`); der Nutzer muss neu verbinden (Connect-Flow
  ersetzt die Tokens im bestehenden Postfach).
- **Löschen:** Tokens liegen nur in `mail_mailboxes.credentials`, das mit dem Postfach gelöscht
  wird. Die Freigabe in Entra entzieht der Nutzer unter <https://myapps.microsoft.com>.
- Tokens, Codes, Secrets und Server-Fehlertexte erscheinen nie in Logs oder Fehlermeldungen; nur
  statische Codes (`authentication_failed`, `access_denied`, `throttled`, …).

Die Token-Hilfen sind bewusst klein und Graph-spezifisch (Microsoft-Endpunkte, PKCE,
`.default`-Scope). Eine gemeinsame OAuth-Basis mit Gmail (#38) lohnt sich, sobald beide
Provider auf `main` sind; der Rückruf `save_credentials` ist bereits provider-neutral.

## 6. Synchronisation

### 5.1 Ordner

`GET /mailFolders` rekursiv (`childFolders`), ohne versteckte Ordner. `remote_id` = Graph-
Ordner-ID, `name` = Pfad (`Posteingang/Projekte`), `parent_id` gesetzt. Rollen über die
Well-known-Namen (`inbox`, `sentitems`, `drafts`, `deleteditems`, `junkemail`, `archive`), in
einem `$batch`-Request aufgelöst. Papierkorb und Junk werden wie bei IMAP angelegt, aber
standardmäßig nicht synchronisiert.

### 5.2 Delta Query pro Ordner

- Referenz: `remote_ref` = **unveränderliche Nachrichten-ID** (`Prefer: IdType="ImmutableId"`).
  Normale Graph-IDs ändern sich beim Verschieben; unveränderliche nicht (innerhalb des
  Postfachs).
- Initialimport: `GET /mailFolders/{id}/messages/delta?$filter=receivedDateTime ge <since>`
  (Standard 90 Tage), Seitengröße `OLLAMAIL_MAIL_SYNC_BATCH_SIZE`. Nach jeder Seite folgt
  `CursorAdvanced` mit dem `@odata.nextLink`, ein abgebrochener Import setzt also an der letzten
  Seite fort. Die letzte Seite liefert den `@odata.deltaLink` für die Folgesyncs.
- Cursor: `{"v": 1, "link": "<nextLink|deltaLink>", "initial": true|false}`.
- Inhalt: MIME-Quelle über `/messages/{id}/$value`. Mails ohne Anhänge werden per JSON-`$batch`
  (bis 20 je Request) geholt, Mails mit Anhängen einzeln (begrenzt den Speicher). Anhänge
  kommen aus der MIME-Quelle, die Normalisierung ist dieselbe wie bei IMAP.
- Flags: `isRead` → `seen`, `flag.flagStatus = flagged` → `flagged`, `isDraft` → `draft`;
  Kategorien sind Keywords (`capabilities.keywords`). `answered` liefert Graph nicht direkt
  (nur über erweiterte MAPI-Eigenschaften, die Delta nicht unterstützt) und fehlt daher.
- Threads: `conversationId` → `provider_thread_id`.
- Ungültiger Delta-Token (`410`, `syncStateNotFound`, `SyncStateInvalid`) →
  `CursorInvalidError`; die Engine importiert den Ordner neu.

### 5.3 Erweiterungen der Sync-Engine (provider-neutral)

Delta Query unterscheidet nicht zwischen „neu“ und „geändert“, und ein Verschieben erscheint
im Quellordner als Löschung. Damit dafür keine Microsoft-Sonderfälle in der Engine nötig sind,
gibt es zwei allgemeine Bausteine:

- **`MessageChanged`** (neues `SyncEvent`): „Diese Mail existiert mit diesen Flags/Ordnern, neu
  oder geändert.“ Kennt die Engine die Mail, wird sie wie `MessageUpdated` behandelt; sonst ruft
  die Engine `event.load()` und speichert die gelieferte `RawMessage` wie `MessageFetched`. So
  wird die MIME-Quelle nur für wirklich neue Mails geladen. Gmail (`history.list`) kann dasselbe
  Ereignis nutzen.
- **Mail ohne synchronisierten Ordner:** Ein `MessageUpdated`, dessen Ordner alle nicht
  synchronisiert werden (z. B. in den Papierkorb verschoben), löscht die Mail – wie bei IMAP, wo
  ein Verschieben dort ohnehin Löschen + neue UID ist (Datenminimierung).

Ein `@removed` im Delta prüft der Provider mit einem `GET` auf `parentFolderId`: existiert die
Mail noch (verschoben), meldet er `MessageUpdated` mit dem neuen Ordner, sonst
`MessageDeleted`. So gehen verschobene Mails weder verloren noch werden sie doppelt
verarbeitet, egal in welcher Reihenfolge die Ordner synchronisiert werden.

Außerdem neu: **`MailboxConfig.save_credentials`**, ein Rückruf, mit dem jeder Provider
erneuerte Zugangsdaten verschlüsselt speichern kann (Engine und Watcher setzen ihn).

### 5.4 Polling vs. Change Notifications

| | Polling (Standard) | Change Notifications (optional) |
|---|---|---|
| Voraussetzung | keine | öffentlich erreichbare HTTPS-URL, Graph muss sie in ≤ 10 s erreichen |
| Latenz | `poll_interval_seconds` (Standard 300 s, je Postfach einstellbar) | Sekunden |
| Last | 1 Delta-Request je Ordner und Intervall | nur bei Änderungen (plus Sicherheits-Polling) |
| Datenfluss | ausgehend | Microsoft ruft die Instanz an (nur IDs, keine Inhalte) |

**Polling:** `watch()` meldet `NotImplementedError`; der Watcher pollt im Intervall. Delta
Requests ohne Änderungen sind billig.

**Change Notifications** (`OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL=https://<host>/api/mail/graph/notifications`):

- `watch()` legt pro Postfach eine Subscription auf `users/{id}/messages` (`created,updated,
  deleted`) an, inklusive `lifecycleNotificationUrl`, verlängert sie rechtzeitig (Laufzeit
  2 Tage, Verlängerung nach einem Tag; Maximum für Mails knapp 7 Tage) und löscht sie beim
  Beenden. Läuft der Watcher in
  mehreren Workern, hält dank Advisory-Lock nur einer die Subscription.
- `clientState` = `<mailbox_id>.<HMAC>` (Schlüssel aus `OLLAMAIL_SECRET_KEY` abgeleitet). Der
  Endpunkt `POST /api/mail/graph/notifications` beantwortet die Validierung
  (`validationToken`), prüft den HMAC jeder Benachrichtigung und stößt nur einen Sync an
  (`request_sync`); Inhalte aus der Benachrichtigung werden nicht verwendet. Ungültige
  Benachrichtigungen werden verworfen (202, damit Graph nicht endlos wiederholt).
- Der Endpunkt ist von der CSRF-Prüfung ausgenommen (kein Cookie, keine Session; die
  Authentizität kommt aus dem HMAC). Ohne konfigurierte URL antwortet er 404.
- Polling läuft trotzdem weiter (verpasste Benachrichtigungen, `missed`-Lifecycle-Events).
- Lehnt Graph die Subscription ab (URL nicht erreichbar, Validierung gescheitert, fehlende
  Berechtigung), protokolliert der Watcher `graph_subscription_failed` und pollt weiter; ein
  neuer Versuch erfolgt beim nächsten Neustart der Überwachung (Worker-Neustart, geänderte
  Postfach-Einstellungen).

## 7. Aktionen

| Interface | Graph |
|---|---|
| `move(ref, folder)` | `POST /messages/{id}/move`; die unveränderliche ID bleibt gleich |
| `set_flags(ref, flags)` | `PATCH /messages/{id}` mit `isRead`, `flag.flagStatus`, `categories` |
| `apply_label` / `remove_label` | Kategorie hinzufügen/entfernen (`PATCH categories`) |

## 8. Fehler, Drosselung, Batching

- **429/503/504:** Warten gemäß `Retry-After` (höchstens 120 s je Versuch, sonst exponentiell),
  bis `OLLAMAIL_MAIL_GRAPH_MAX_RETRIES`; danach `ConnectionFailedError` (`throttled`) → der Job
  wird mit Backoff wiederholt. Gilt auch für Teilantworten in `$batch`.
- **401** → einmal Token erneuern, dann `AuthenticationError`. **403** → `access_denied`
  (fehlende Berechtigung, Application Access Policy). **404** bei Aktionen →
  `MessageNotFoundError`. Netzwerkfehler → `ConnectionFailedError`.
- `$batch` für MIME-Abrufe und Ordnerrollen (max. 20 Anfragen je Batch, Graph-Limit).
- Graph erlaubt 4 parallele Anfragen je Postfach und App; der Provider arbeitet sequentiell.

## 9. Datenschutz

- Delegiert: Zugriff nur auf das eigene Postfach. App-only: nur mit RBAC for Applications /
  Application Access Policy (siehe 3.2) – das ist in der Betreiber-Doku Pflicht.
- Tokens und Client-Secret: verschlüsselt bzw. nur in der Umgebung. Logs enthalten nur IDs und
  Fehlercodes, nie Adressen, Betreffzeilen, Ordnernamen, Tokens oder Graph-Fehlertexte.
- Change Notifications enthalten nur Ressourcen-IDs (keine `includeResourceData`).
- Löschung: Postfach löschen entfernt Tokens, Mails, Anhänge; eine aktive Subscription läuft
  spätestens nach ihrer Laufzeit ab.

## 10. Tests

- **Contract-Tests** (`backend/tests/mail/test_graph_*.py`): synthetische Graph-Antworten mit
  `respx` (Struktur wie in der Graph-Doku, Daten frei erfunden, `example.com`). Abgedeckt:
  Ordner und Rollen, Initial-Delta mit Paging, Fortsetzen, inkrementelle Änderungen, Verschieben,
  Löschen, ungültiger Delta-Token, `$batch` inkl. Teil-Drosselung, 429 mit `Retry-After`,
  Token-Refresh inkl. Speichern, App-only-Token, Aktionen, Subscriptions, Webhook-Endpunkt,
  Connect-Flow, Senden (`test_graph_send.py`: `createReply`/`createReplyAll` + `send`, Token
  mit `Mail.Send`, fehlende Berechtigung, keine Wiederholung).
- Engine-Tests für `MessageChanged` und „kein synchronisierter Ordner“.

### Manuelle Testanleitung mit einem M365-Developer-Tenant

1. Tenant besorgen: <https://developer.microsoft.com/microsoft-365/dev-program> (Sandbox mit
   Testnutzern und Beispielmails). Keine Produktivdaten verwenden.
2. App wie in Abschnitt 2 registrieren, Redirect-URI `http://localhost:8080/api/mail/graph/callback`
   (für lokale Tests erlaubt Entra `http://localhost`).
3. `.env`: `OLLAMAIL_MAIL_GRAPH_CLIENT_ID`, `…_CLIENT_SECRET`, `…_TENANT_ID=<tenant-id>`,
   `OLLAMAIL_AUTH_COOKIE_SECURE=false` (nur lokal). `docker compose up`.
4. Anmelden, dann im Browser-DevTools-Fenster (bis die UI kommt):
   ```js
   const csrf = document.cookie.match(/ollamail_csrf=([^;]+)/)[1];
   const r = await fetch("/api/mail/graph/connect", {method: "POST", headers: {"X-CSRF-Token": csrf, "Content-Type": "application/json"}, body: "{}"});
   location = (await r.json()).authorization_url;
   ```
   Mit einem Testnutzer anmelden und zustimmen; Rücksprung mit `?graph=connected`.
5. Worker-Log: `mail_watch_started`, `mail_sync_finished` mit `stored > 0`. Prüfen:
   - Mail in Outlook als gelesen markieren → nach dem nächsten Poll `updated = 1`.
   - Mail in einen anderen Ordner verschieben → keine neue Verarbeitung, Ordner wechselt.
   - Mail löschen (Papierkorb) → Mail verschwindet aus ollamail.
6. App-only: Sicherheitsgruppe mit einem Testpostfach anlegen, RBAC for Applications wie in 3.2,
   Postfach mit `{"auth": "application", "user": "<adresse>"}` anlegen. Gegenprobe: ein
   Postfach außerhalb der Gruppe meldet `access_denied`.
7. Webhooks (optional): z. B. mit einem Tunnel (`cloudflared tunnel --url http://localhost:8080`)
   `OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL=https://<tunnel>/api/mail/graph/notifications` setzen;
   neue Mail → Sync innerhalb weniger Sekunden. Anschließend die Variable wieder entfernen.
8. Aufräumen: Postfach in ollamail löschen, App-Zustimmung unter <https://myapps.microsoft.com>
   entfernen.
