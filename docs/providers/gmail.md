# Mail-Provider Gmail / Google Workspace

Status: umgesetzt mit #38. Code: `backend/app/mail/providers/gmail*.py`.
Allgemeines Provider-Konzept: [`ARCHITECTURE.md` §3.1](../ARCHITECTURE.md#31-mail-provider).

Der Gmail-Provider spricht die **Gmail REST API** (nicht IMAP). Er implementiert das
`MailProvider`-Interface und nutzt die gemeinsame Sync-Engine (`app/mail/sync/`). Gmail-
Besonderheiten bleiben im Provider; die Engine kennt nur den neuen, provider-neutralen
Erweiterungspunkt „postfachweiter Cursor“ (siehe 5).

## 1. Ziele und Randbedingungen

- **Self-Hosting ohne öffentliche URL ist der Normalfall.** Der OAuth-Redirect muss nur vom
  Browser des Nutzers erreichbar sein (z. B. `http://localhost:8080/...` oder eine interne URL),
  nicht aus dem Internet. Änderungen werden standardmäßig per **Polling** abgeholt; Pub/Sub ist
  optional und braucht ein GCP-Projekt.
- **Keine Cloud-Pflicht für ollamail selbst:** Es wird nur die Gmail-API des Postfachs
  angesprochen, sonst nichts. Kein Google-SDK, nur `httpx` und `cryptography`.
- **Datenschutz:** Tokens verschlüsselt (`EncryptedJSON`), Access-Tokens nur im Speicher,
  keine Mail-Inhalte, Betreffzeilen, Adressen oder Google-Fehlertexte in Logs und
  `SyncState.last_error` (nur Fehlercodes).

## 2. Authentifizierung

Zwei Modi, gewählt per `provider_settings.auth` des Postfachs:

| Modus | Für | Zugangsdaten |
|---|---|---|
| `oauth` (Standard) | Einzelne Nutzer (Gmail und Workspace) | Refresh-Token des Nutzers in `credentials.refresh_token` |
| `service_account` | Workspace, org-weiter Rollout | Service Account mit **Domain-wide Delegation**, Schlüsseldatei der Instanz |

### 2.1 OAuth-Client (Modus `oauth`)

Der Admin legt in der Google Cloud Console einen OAuth-Client vom Typ **Webanwendung** an und
trägt die Redirect-URI ein:

```
<Basis-URL von ollamail>/api/mail/gmail/oauth/callback
```

Google erlaubt für Webanwendungen `http://localhost[:port]` ohne HTTPS; jede andere URI braucht
HTTPS. Eine öffentliche Erreichbarkeit ist **nicht** nötig – Google leitet nur den Browser um.

Ablauf (Authorization Code Flow mit PKCE, `access_type=offline`, `prompt=consent`):

1. `POST /mail/gmail/oauth/start` (angemeldet, CSRF-geschützt) erzeugt `state` und PKCE-Verifier,
   legt beides HMAC-signiert in einem kurzlebigen `HttpOnly`-Cookie (Pfad
   `/api/mail/gmail/oauth`, 10 Minuten, `SameSite=Lax`) ab und gibt die Google-URL zurück.
2. Der Browser meldet sich bei Google an und stimmt zu.
3. `GET /mail/gmail/oauth/callback` prüft Cookie, `state` und Nutzer, tauscht den Code gegen
   Tokens, prüft die **tatsächlich gewährten Scopes** (Google erlaubt dem Nutzer, einzelne
   Scopes abzuwählen), liest die Adresse über `users/me/profile` und legt das Postfach an bzw.
   aktualisiert den Refresh-Token eines bestehenden Gmail-Postfachs desselben Nutzers mit
   derselben Adresse. Danach Weiterleitung auf `/?mailbox_connected=<id>` bzw.
   `/?mailbox_error=<code>`.

Gespeichert wird nur der Refresh-Token (verschlüsselt). Access-Tokens (1 h gültig) liegen nur
im Speicher des Prozesses (Cache je Postfach, Erneuerung 2 Minuten vor Ablauf). Widerruft der
Nutzer den Zugriff (`invalid_grant`), meldet der Sync `token_revoked`; der Nutzer verbindet das
Postfach über denselben Flow neu.

Die Postfach-Verwaltung (Liste, Löschen, Einstellungen) kommt mit der Postfach-API (#15); der
Connect-Flow ist bewusst minimal und additiv.

### 2.2 Scopes und Google-Verifizierung

| Scope | Wann | Einstufung |
|---|---|---|
| `https://www.googleapis.com/auth/gmail.modify` | Standard: Lesen plus Aktionen (Labels, Archivieren, Gelesen-Status) und Senden von Antworten (#92) | restricted |
| `https://www.googleapis.com/auth/gmail.readonly` | `OLLAMAIL_GMAIL_READONLY=true`: nur Lesen, Aktionen und Senden liefern `read_only` | restricted |

Beide Scopes sind **restricted**. Folgen:

- **Workspace:** OAuth-Zustimmungsbildschirm als **„Intern“** anlegen. Interne Apps brauchen
  keine Google-Verifizierung und keinen Security-Assessment; nur Nutzer der eigenen Domain
  können sich verbinden. Das ist der empfohlene Weg für Unternehmen.
- **Privates Gmail:** App als „Extern“ im Status **„Testing“** betreiben und die eigenen
  Adressen als Testnutzer eintragen (max. 100). Achtung: Refresh-Tokens von Apps im Status
  „Testing“ laufen nach **7 Tagen** ab – dann neu verbinden. Für einen dauerhaften Betrieb mit
  externen Nutzern verlangt Google eine Verifizierung inklusive jährlichem
  Security-Assessment (CASA); das ist für Self-Hosting meist unverhältnismäßig.
- ollamail fordert **keine** weiteren Scopes an (kein `openid`, kein Profil, kein eigener
  `gmail.send`: `gmail.modify` schließt das Senden ein). Die Adresse kommt aus
  `users/me/profile`. Bestehende Verbindungen müssen für das Senden also nicht erneuert werden.
  Den Scope `tasks` fordert nur der eigene Connect-Flow des Aufgaben-Exports an (§8).

### 2.3 Domain-wide Delegation (Modus `service_account`)

Für org-weite Rollouts ohne Zustimmung jedes einzelnen Nutzers:

1. In GCP einen Service Account anlegen, JSON-Schlüssel erzeugen.
2. In der Google Admin Console unter *Sicherheit → API-Steuerung → Domainweite Delegierung*
   die Client-ID des Service Accounts mit dem Scope `gmail.modify` (bzw. `gmail.readonly`)
   freigeben.
3. Schlüsseldatei als Docker-Secret/Volume einbinden und `OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE`
   auf den Pfad setzen. Die Datei ist ein Secret und gehört nie ins Repo.
4. Postfächer mit `provider_settings = {"auth": "service_account"}` anlegen (Postfach-API #15).

Der Provider signiert ein JWT (RS256) mit `sub = <Postfachadresse>` und tauscht es bei
`token_uri` gegen ein Access-Token. Ein Fehler beim Tausch (Delegation fehlt, Nutzer existiert
nicht) wird zu `authentication_failed`. Domain-wide Delegation gibt dem Schlüssel Zugriff auf
**alle** Postfächer der Domain – den Scope in der Admin Console so eng wie möglich halten und
den Schlüssel wie ein Root-Passwort behandeln.

## 3. Labels ↔ Ordner

Gmail kennt nur Labels; eine Mail kann mehrere tragen. Abbildung (`RemoteFolder.kind = label`):

| Gmail | `remote_id` | Rolle | Bemerkung |
|---|---|---|---|
| `INBOX` | `INBOX` | `inbox` | |
| `SENT` | `SENT` | `sent` | |
| `DRAFT` | `DRAFT` | `drafts` | |
| `SPAM` | `SPAM` | `junk` | Standard: nicht synchronisiert |
| `TRASH` | `TRASH` | `trash` | Standard: nicht synchronisiert |
| – (virtuell, „Alle Nachrichten“) | `ALL_MAIL` | `all` | jede Mail außerhalb von Spam/Papierkorb |
| Nutzer-Labels | Label-ID (`Label_12`) | – | Name wie in Gmail (`Projekte/Alpha`); `parent_id` aus dem Pfad |
| `UNREAD`, `STARRED` | – | – | werden zu Flags (`seen`, `flagged`) |
| `IMPORTANT`, `CHAT`, `CATEGORY_*` | – | – | ignoriert (keine Ordner) |

`ALL_MAIL` entspricht Gmails „Alle Nachrichten“: Archivierte Mails (ohne `INBOX`) bleiben so in
genau einem synchronisierten Ordner sichtbar. Flags: `seen` ⇔ kein `UNREAD`, `flagged` ⇔
`STARRED`, `draft` ⇔ `DRAFT`. `answered` gibt es in Gmail nicht. Mails mit dem Label `CHAT`
(Hangouts-Verläufe) werden nicht importiert.

Mails nur in Spam/Papierkorb liefert der Provider standardmäßig nicht (wie
`includeSpamTrash=false`); mit `provider_settings.include_spam_trash = true` schon – dann
entscheiden die normalen Ordner-Ausschlüsse der Sync-Einstellungen.

## 4. Aktionen

| Interface | Gmail |
|---|---|
| `set_flags` | `messages.modify`: `UNREAD`/`STARRED` setzen bzw. entfernen |
| `apply_label` / `remove_label` | `messages.modify` mit Label-ID; Label wird per ID oder Name gefunden, fehlende Labels legt `apply_label` an (`labels.create`) |
| `move(ref, "ALL_MAIL")` | **Archivieren**: `INBOX` entfernen |
| `move(ref, "TRASH")` | `messages.trash` |
| `move(ref, <Label>)` | Ziel-Label hinzufügen, `INBOX`/`SPAM`/`TRASH` entfernen |
| `send(reply)` | `messages.send` mit der RFC-5322-Quelle (`raw`, Base64url) und der `threadId` der beantworteten Mail; `In-Reply-To`, `References` und `Re:`-Betreff halten die Antwort in Gmails Thread. Gmail legt die Kopie unter `SENT` ab, der Sync bringt sie. Keine automatische Wiederholung (auch nicht bei 5xx), damit nichts doppelt gesendet wird; fehlender Scope → `send_not_permitted`, abgelehnte Mail → `message_refused` |

`remote_ref` ist die Gmail-Message-ID und bleibt bei allen Aktionen gleich.

## 5. Synchronisation

### 5.1 Postfachweiter Cursor (Erweiterung der Engine)

Gmail führt **ein** Änderungsprotokoll für das ganze Postfach (`historyId`), nicht eines je
Ordner. Dafür hat die Engine einen provider-neutralen Erweiterungspunkt bekommen:

- `ProviderCapabilities.mailbox_cursor = True` ⇒ die Engine ruft pro Sync **einmal**
  `fetch_since(MAILBOX_SCOPE, cursor)` auf (statt einmal je Ordner) und speichert den Cursor in
  der postfachweiten Zeile von `mail_sync_states` (`folder_id IS NULL`). Das Schema hatte diesen
  Fall bereits vorgesehen; es ist **keine Migration** nötig.
- Die Engine speichert nur Mails, deren `folder_ids` mindestens einen synchronisierten Ordner
  enthalten. Verliert eine gespeicherte Mail ihren letzten synchronisierten Ordner (z. B. in
  den Papierkorb verschoben), wird sie lokal gelöscht. Ordner-Ausschlüsse
  (`excluded_roles`, `excluded_folders`) gelten also auch hier.
- **Resync bei ungültigem Cursor:** Die Referenzen (Message-IDs) bleiben gültig, deshalb wird
  nicht alles gelöscht und neu importiert (das würde Triage, Todos usw. verwerfen). Stattdessen
  importiert die Engine den Zeitraum neu (bekannte Mails werden nur aktualisiert, die Hooks
  feuern nur für wirklich neue Mails) und löscht danach lokale Mails dieses Zeitraums, die der
  Server nicht mehr geliefert hat. Der neue Cursor wird erst am Ende gespeichert; ein
  abgebrochener Resync beginnt beim nächsten Lauf von vorn. Mails, die älter als der Zeitraum
  sind, bleiben unverändert erhalten.

Provider mit postfachweitem Cursor müssen stabile Referenzen haben (Gmail, Graph, JMAP).

### 5.2 Cursor-Format

```json
{"v": 1, "history_id": "123456", "import": {"q": "after:1719792000", "page_token": "..."}}
```

`import` fehlt, sobald der Initialimport fertig ist.

### 5.3 Initialimport

1. `users/me/profile` → aktuelle `historyId` **vor** dem Auflisten merken (Änderungen während
   des Imports holt danach `history.list`).
2. `messages.list` mit `q=after:<Unix-Zeit>` (Zeitraum aus den Sync-Einstellungen, Standard 90
   Tage), neueste zuerst, Seitengröße `OLLAMAIL_MAIL_SYNC_BATCH_SIZE`.
3. Je Seite Abruf der Quellen (`format=raw`) per **Batch-Request** (bis zu 20 Teilanfragen je
   Batch, begrenzt den Speicherbedarf), dann `CursorAdvanced` mit dem nächsten `pageToken`. Ein
   abgebrochener Import setzt an der letzten Seite fort; ist der `pageToken` nicht mehr gültig,
   beginnt die Liste neu (bereits gespeicherte Mails werden nur aktualisiert).

### 5.4 Inkrementell über `history.list`

`history.list?startHistoryId=<h>&historyTypes=messageAdded,messageDeleted,labelAdded,labelRemoved`,
seitenweise. Je Seite wird pro Mail der Netto-Zustand gebildet:

- `messagesAdded` → Quelle per Batch laden → `MessageFetched` (verschwindet die Mail
  zwischendurch, `MessageDeleted`),
- `messagesDeleted` → `MessageDeleted`,
- `labelsAdded`/`labelsRemoved` → `MessageUpdated` (Flags und Ordner aus den aktuellen
  `labelIds`); landet die Mail in Spam/Papierkorb → `MessageDeleted`; kommt sie von dort
  zurück → Quelle laden → `MessageFetched`.

Nach jeder Seite `CursorAdvanced` mit der ID des letzten Eintrags, am Ende mit der `historyId`
der Antwort. Ein offener Initialimport läuft danach weiter.

**Abgelaufene `historyId`:** Google hält die History nur begrenzt vor (typisch etwa eine Woche,
teils kürzer). `history.list` antwortet dann mit `404`; der Provider wirft
`CursorInvalidError` und die Engine macht den Resync aus 5.1.

### 5.5 Polling (Standard) und Pub/Sub (optional)

- **Polling:** Ohne Pub/Sub-Konfiguration meldet der Provider `push = False`; der Watcher stößt
  alle `poll_interval_seconds` (Standard 300 s) einen Sync an. Ein Sync ohne Änderungen kostet
  einen `history.list`-Aufruf (2 Quota-Einheiten).
- **Pub/Sub (Pull):** Es ist **keine öffentliche URL** nötig, weil ollamail die Benachrichtigungen
  per *Pull* abholt. Voraussetzungen:
  1. GCP-Projekt mit Topic, Publisher-Recht für `gmail-api-push@system.gserviceaccount.com`.
  2. **Eine Pull-Subscription je Postfach** (Benachrichtigungen enthalten keine filterbaren
     Attribute; Nachrichten für fremde Adressen gibt der Provider sofort zurück, aber eine
     geteilte Subscription verzögert die Zustellung).
  3. Service Account mit `roles/pubsub.subscriber` auf der Subscription; Schlüssel in
     `OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE` (derselbe wie für Domain-wide Delegation möglich).
  4. Im Postfach: `provider_settings.pubsub_topic = "projects/<p>/topics/<t>"` und
     `pubsub_subscription = "projects/<p>/subscriptions/<s>"`.

  `watch()` ruft `users.watch` auf (Erneuerung täglich, Google verlangt spätestens alle 7 Tage),
  holt Nachrichten per `subscriptions.pull`, bestätigt eigene (`acknowledge`) und meldet je
  Benachrichtigung ein `ChangeEvent`. Polling läuft als Rückfallebene weiter.

### 5.6 Quota, Rate-Limits, Fehler

- Gmail-Quota: 250 Einheiten je Nutzer und Sekunde (`messages.get` = 5, `messages.list` = 5,
  `history.list` = 2, `messages.modify` = 5). Batches zählen jede Teilanfrage einzeln.
- `429`, `403` mit `rateLimitExceeded`/`userRateLimitExceeded` und `5xx` werden mit
  exponentiellem Backoff (beachtet `Retry-After`, max. 5 Versuche) wiederholt; auch einzelne
  fehlgeschlagene Teilanfragen eines Batches.
- `401` → Token einmal erneuern und wiederholen, dann `authentication_failed`.
  `403` wegen fehlender Rechte → `insufficient_scope`. Netzwerkfehler → `connection_failed`
  (Job-Retry).
- Fehlercodes in `last_error` (bzw. `mailbox_error` im Connect-Flow):

  | Code | Bedeutung |
  |---|---|
  | `authentication_failed` | Token abgelehnt (auch nach Erneuerung) |
  | `token_revoked` | Refresh-Token widerrufen/abgelaufen (`invalid_grant`) → neu verbinden |
  | `credentials_missing` | Kein Refresh-Token gespeichert |
  | `insufficient_scope` | Gmail-Scope nicht gewährt |
  | `access_denied` | Sonstiges 403 (z. B. Konto gesperrt) bzw. Abbruch bei Google |
  | `delegation_denied` | Domain-wide Delegation für Service Account/Scope fehlt |
  | `oauth_not_configured` | `OLLAMAIL_GMAIL_CLIENT_ID`/`_SECRET` fehlen |
  | `service_account_missing`, `service_account_invalid` | Schlüsseldatei fehlt bzw. ist ungültig |
  | `api_disabled` | Gmail API im GCP-Projekt nicht aktiviert |
  | `invalid_configuration` | `provider_settings` ungültig |
  | `connection_failed`, `rate_limited`, `server_error`, `token_endpoint_unavailable` | vorübergehend, der Job wird wiederholt |
  | `read_only` | Aktion bei `OLLAMAIL_GMAIL_READONLY=true` |
  | `invalid_state`, `refresh_token_missing` | Connect-Flow: Cookie/`state` ungültig bzw. Google lieferte keinen Refresh-Token |

## 6. Konfiguration

Instanz (`OLLAMAIL_GMAIL_*`, siehe `deploy/.env.example`):

| Variable | Bedeutung |
|---|---|
| `OLLAMAIL_GMAIL_CLIENT_ID` / `OLLAMAIL_GMAIL_CLIENT_SECRET` | OAuth-Client (Modus `oauth`) |
| `OLLAMAIL_GMAIL_REDIRECT_URI` | Exakt die in Google eingetragene Callback-URL |
| `OLLAMAIL_GMAIL_READONLY` | `true` = Scope `gmail.readonly`, keine Aktionen |
| `OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE` | Pfad zur Service-Account-Schlüsseldatei (Delegation, Pub/Sub) |
| `OLLAMAIL_GMAIL_TIMEOUT` | Sekunden je API-Anfrage (Standard 60) |

Postfach (`provider_settings`): `auth` (`oauth`/`service_account`), `include_spam_trash`,
`pubsub_topic`, `pubsub_subscription`.

## 7. Tests

- **Contract-Tests** (`backend/tests/mail/test_gmail_*.py`) mit synthetischen, an die
  Gmail-API-Dokumentation angelehnten Antworten (`respx`): Labels, Initialimport mit Batch,
  History inkl. `404`, Aktionen, Token-Refresh, Service-Account-JWT, Rate-Limits, Pub/Sub-Pull,
  Connect-Flow, Senden (`test_gmail_send.py`). Keine echten Daten, keine Secrets.
- **Engine-Tests** für den postfachweiten Cursor mit `FakeMailProvider(mailbox_cursor=True)`.

### 7.1 Manuelle Testanleitung

Mit einem **Test-Konto** (nie mit echten Postfächern Dritter):

1. GCP-Projekt anlegen, *Gmail API* aktivieren.
2. OAuth-Zustimmungsbildschirm: Workspace → „Intern“; privates Konto → „Extern“, Status
   „Testing“, eigene Adresse als Testnutzer.
3. OAuth-Client „Webanwendung“ mit Redirect-URI
   `http://localhost:8080/api/mail/gmail/oauth/callback` anlegen.
4. In `deploy/.env`: `OLLAMAIL_GMAIL_CLIENT_ID`, `OLLAMAIL_GMAIL_CLIENT_SECRET`,
   `OLLAMAIL_GMAIL_REDIRECT_URI` setzen; `docker compose up -d`.
5. Anmelden, dann in der Browser-Konsole (CSRF-Token aus dem Cookie `ollamail_csrf`):
   ```js
   const csrf = document.cookie.match(/ollamail_csrf=([^;]+)/)[1];
   const r = await fetch('/api/mail/gmail/oauth/start', {method: 'POST',
     headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: '{}'});
   location.href = (await r.json()).authorization_url;
   ```
6. Bei Google zustimmen (alle Häkchen setzen). Erwartet: Weiterleitung auf
   `/?mailbox_connected=<id>`. Mit abgewähltem Gmail-Häkchen: `/?mailbox_error=insufficient_scope`.
7. Innerhalb einer Minute startet der Watcher den ersten Sync. Prüfen:
   `docker compose logs worker | grep mail_sync_finished` (nur Zähler, keine Inhalte).
8. Im Test-Postfach eine Mail empfangen, eine archivieren, eine löschen, ein Label vergeben;
   nach spätestens `poll_interval_seconds` sind die Änderungen in der DB
   (`mail_messages`, `mail_message_folders`).
9. Resync testen: in der DB `UPDATE mail_sync_states SET cursor = '{"v": 1, "history_id": "1"}'
   WHERE folder_id IS NULL;` → nächster Sync meldet `mail_sync_cursor_invalid` und gleicht ab,
   ohne bestehende Mails neu anzulegen.
10. Zugriff unter <https://myaccount.google.com/permissions> widerrufen → nächster Sync:
    `last_error = token_revoked`.
11. Optional Pub/Sub: Topic + Subscription anlegen, `provider_settings` ergänzen,
    `OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE` setzen; neue Mails lösen innerhalb weniger Sekunden
    einen Sync aus (`mail_sync_finished` ohne auf das Polling-Intervall zu warten).

## 8. Google Tasks (Aufgaben-Export, #102)

Der Aufgaben-Export (`backend/app/todos/export/`, [`ARCHITECTURE.md` §4.3](../ARCHITECTURE.md#43-todos))
kann Aufgaben nach **Google Tasks** übertragen (Sink `gtasks`, Code `gtasks.py`,
`gtasks_connect.py`). Er nutzt denselben OAuth-Client wie der Gmail-Provider und dessen
Bausteine (PKCE, signiertes `state`-Cookie, Token-Austausch und -Erneuerung aus
`gmail_auth.py`, Retries und Fehlercodes aus `gmail_api.py`); ein Gmail-Postfach ist dafür
**nicht** nötig.

### 8.1 Einrichtung (Admin)

1. Im GCP-Projekt des OAuth-Clients zusätzlich die **Google Tasks API** aktivieren.
2. OAuth-Zustimmungsbildschirm: Scope `https://www.googleapis.com/auth/tasks` hinzufügen.
   Er ist **sensitive** (nicht restricted): Für „Intern“ (Workspace) und „Testing“ ist keine
   Verifizierung nötig; ein externer Produktivbetrieb braucht die normale
   Google-Verifizierung, aber kein Security-Assessment. Für „Testing“ gilt auch hier: Der
   Refresh-Token läuft nach 7 Tagen ab, dann neu verbinden.
3. Beim OAuth-Client eine **zweite Redirect-URI** eintragen:
   ```
   <Basis-URL von ollamail>/api/todo-export/gtasks/oauth/callback
   ```
   Ohne `OLLAMAIL_TODOS_EXPORT_GTASKS_REDIRECT_URI` leitet ollamail sie aus
   `OLLAMAIL_GMAIL_REDIRECT_URI` ab (gleicher Präfix, `/mail/gmail/oauth/callback` →
   `/todo-export/gtasks/oauth/callback`).
4. `OLLAMAIL_TODOS_EXPORT_SINKS` um `gtasks` ergänzen (z. B. `caldav,gtasks`) und
   `OLLAMAIL_GMAIL_CLIENT_ID`/`_SECRET` setzen.

Domain-wide Delegation wird für Google Tasks bewusst **nicht** genutzt: Jeder Nutzer stimmt
selbst zu, der Service-Account-Schlüssel bekommt keinen weiteren Scope.

### 8.2 Verbinden (Nutzer)

Unter Einstellungen → Aufgaben-Export „Google Tasks“ wählen, Modus wählen, „Mit Google
verbinden“:

1. `POST /todo-export/gtasks/oauth/start` (nur wenn `gtasks` freigegeben ist) liefert die
   Google-URL mit Scope `tasks`, `include_granted_scopes=true`, `access_type=offline`,
   `prompt=consent` und PKCE. `state`, Verifier, Nutzer und Modus liegen HMAC-signiert im
   `HttpOnly`-Cookie `ollamail_gtasks_oauth` (10 Minuten, eigener Zweck `todo_export_gtasks`,
   damit ein Cookie des Gmail-Flows hier nicht gilt).
2. `GET /todo-export/gtasks/oauth/callback` prüft Cookie, `state` und Nutzer, tauscht den
   Code, prüft, dass `tasks` **tatsächlich gewährt** wurde, und holt die Aufgabenlisten. Ein
   neues Ziel exportiert in die erste Liste (Googles Standardliste „Meine Aufgaben“); eine
   andere Liste wählt der Nutzer danach (`GET /todo-export/lists`,
   `PATCH /todo-export {"list_id"}`). Erneutes Verbinden desselben Kontos (seine Liste gibt es
   noch) ersetzt nur den Refresh-Token; ein anderes Konto beginnt neu (Verweise werden
   verworfen). Weiterleitung auf `/settings/task-export?gtasks=connected` bzw.
   `?gtasks_error=<code>`.

Gespeichert wird nur der Refresh-Token, verschlüsselt in `todo_export_targets.config`
(`EncryptedJSON`); Access-Tokens nur im Prozessspeicher. Logs nur mit Nutzer- und Ziel-ID und
Fehlercode. Der Formular-Weg (`PUT /todo-export`, `POST /todo-export/lists` mit Zugangsdaten)
lehnt `gtasks` mit `oauth_required` ab.

### 8.3 Abbildung

| ollamail | Google Tasks |
|---|---|
| Titel | `title` (max. 1024 Zeichen) |
| Beschreibung, Link zur Mail | `notes` (max. 8192 Zeichen; Beschreibung wird gekürzt), am Ende die Markierung `[ollamail:<Todo-ID>]` |
| Fälligkeit | `due` (nur Datum; Google ignoriert die Uhrzeit) |
| offen / erledigt / verworfen | `needsAction` / `completed` / `completed` (Google kennt kein „abgebrochen“) |
| Priorität | – (Google Tasks kennt keine Priorität) |

`links` ist in der API nur lesbar; der Link zur Mail steht deshalb in `notes`.

- **Keine Duplikate:** Google vergibt die Task-IDs selbst. `push` sucht die Todo-ID deshalb
  zuerst über die Markierung in der Liste (eine Abfrage pro Abgleich) und überschreibt einen
  Treffer; das Anlegen selbst wird nach einer verlorenen Antwort nie automatisch wiederholt.
- **Ändern** per `PATCH` mit `If-Match: <etag>`; `412` ist ein Konflikt, den der Abgleich
  wie bei CalDAV auflöst.
- **Statusabgleich** über `tasks.list` mit `showCompleted`, `showHidden` und `showDeleted`:
  Aufgaben mit anderem `etag` haben sich geändert, gelöschte oder fehlende sind im Ziel
  gelöscht. `updatedMin` wird nicht genutzt, weil der Sink zwischen zwei Läufen keinen Zustand
  hält und eine endgültig entfernte Aufgabe sonst nicht von einer unveränderten zu
  unterscheiden wäre.
- Wird eine **verworfene** Aufgabe in Google Tasks später bearbeitet, kommt sie als
  „erledigt“ zurück (Google kennt nur diese zwei Zustände).

### 8.4 Fehler

`401` → Token einmal erneuern, dann `auth_failed` (UI: neu verbinden); `429`, `403`
mit Rate-Limit und `5xx` → Backoff (wie Gmail), danach `unavailable`; `404` der Liste →
`list_not_found`; Tasks API nicht aktiviert → `api_disabled`; kein OAuth-Client →
`oauth_not_configured`. Connect-Flow zusätzlich: `consent_denied` (abgebrochen), `insufficient_scope`,
`invalid_state`, `token_revoked`, `refresh_token_missing`, `no_lists`, `sink_not_available`.

### 8.5 Tests und manuelle Prüfung

`backend/tests/todos/export/test_gtasks.py` (Sink gegen die mit `respx` gemockte Tasks API:
Listen, Anlegen, Wiederanlegen ohne Duplikat, Konflikt, gelöschte Aufgaben, 401/429, ein
Abgleich mit dem echten Sync) und `test_gtasks_connect.py` (Connect-Flow mit PostgreSQL).
Manuell mit einem **Test-Konto**: Einrichtung wie 8.1, verbinden, eine Aufgabe in ollamail
anlegen (erscheint in „Meine Aufgaben“), in Google Tasks abhaken (nach spätestens
`OLLAMAIL_TODOS_EXPORT_POLL_MINUTES` erledigt), unter
<https://myaccount.google.com/permissions> widerrufen → Status „Google hat den Zugriff
abgelehnt“.
