# Datenschutz & Sicherheit (DSGVO by design)

ollamail verarbeitet hochsensible personenbezogene Daten (E-Mails Dritter). Datenschutz ist kein
Feature, sondern eine Randbedingung für jede Änderung.

## Grundsätze

1. **Datenminimierung** – Es wird nur synchronisiert, was konfiguriert ist (Postfächer, Ordner, Zeitraum).
   Standard-Erstimport: 90 Tage, einstellbar.
2. **Lokale Verarbeitung** – Standardmäßig verlassen keine Daten die Instanz. Cloud-LLMs sind deaktiviert,
   bis der Admin sie ausdrücklich freigibt. Bei Freigabe zeigt die UI dauerhaft an, welche Daten an
   welchen Anbieter gehen.
3. **Zweckbindung & Trennung** – Daten eines Nutzers werden nie für einen anderen genutzt
   (keine nutzerübergreifenden Few-Shot-Beispiele, RAG-Zugriff strikt per SQL-Filter).
4. **Admin ≠ Leser** – Admins verwalten die Instanz, sehen aber **keine fremden Mail-Inhalte**,
   nur Metadaten (Anzahl, Sync-Status, Fehler) und aggregierte Statistiken.
5. **Transparenz** – Jede KI-Bewertung (Triage, Todo) ist für den Nutzer erklärbar und korrigierbar.

## Technische Maßnahmen

| Thema | Maßnahme |
|---|---|
| Secrets | IMAP-Passwörter, OAuth-Tokens, IdP-Client-Secrets, LDAP-Bind-Passwörter: AES-256-GCM, Envelope-Encryption mit Master-Key aus `OLLAMAIL_SECRET_KEY` (Key-Rotation unterstützt) |
| At rest | Empfehlung: verschlüsseltes Volume/Dateisystem. Optional: Verschlüsselung von Mail-Bodies/Anhängen auf Anwendungsebene (Feature-Flag) |
| Microsoft 365 | OAuth-Tokens verschlüsselt (`mail_mailboxes.credentials`), Client-Secret nur in der Umgebung. App-only-Zugriff nur mit Einschränkung auf freigegebene Postfächer (RBAC for Applications / `ApplicationAccessPolicy`, siehe `docs/providers/microsoft365.md`). Change Notifications optional, ohne Inhalte (nur IDs, `clientState` per HMAC geprüft) |
| In transit | TLS für IMAP/LDAP/OIDC Pflicht (Ausnahme nur explizit per Admin-Setting, IMAP: `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS`), HTTPS hinter Reverse Proxy |
| Logs | **Keine** Betreffzeilen, Adressen, Inhalte, Prompts oder LLM-Antworten in Logs. IDs statt Inhalte. Ein Log-Filter erzwingt das. |
| Job-Queue | Job-Argumente enthalten nur IDs, keine Inhalte. Abgeschlossene Jobs werden nach 7 Tagen gelöscht. Procrastinate-Logs werden auf statische Event-Namen reduziert (keine Argumente, keine Rückgabewerte) |
| Echtzeit-Events | Payload nur Typ, IDs und Status (per Pattern erzwungen); Zustellung ausschließlich an den betroffenen Nutzer |
| Audit-Log | Append-only und hash-verkettet: Login (Erfolg/Fehlschlag), Logout, Setup, Session-Widerruf, Nutzer angelegt, Rollenänderung, IdP- und KI-Einstellungen, Postfach angelegt/entfernt/freigegeben, Export, Löschung, Key-Rotation. Nur IDs und Codes, keine Inhalte (siehe unten) |
| Sessions | Serverseitig, widerrufbar, Lebensdauer und Idle-Timeout konfigurierbar. In der DB nur der SHA-256 des Cookie-Tokens; Cookies `HttpOnly`, `Secure`, `SameSite=Lax`; CSRF-Schutz per signiertem Double-Submit-Token |
| Passwörter | Argon2id (RFC 9106); Rate-Limit und Kontosperre in Postgres. Die Zähler speichern nur HMACs von IP-Adresse bzw. E-Mail-Adresse und werden stündlich bereinigt |
| Telemetrie | Keine. Keine externen Fonts/CDNs im Frontend. Die eingebaute Telemetrie von ONNX Runtime (von Piper genutzt) ist per `ORT_DISABLE_TELEMETRY=1` abgeschaltet, im Code und im Image (Test: `tests/ai/tts/test_piper.py`) |
| Triage | Few-Shot-Beispiele nur aus Korrekturen desselben Nutzers in eigenen Postfächern (doppelt gefiltert, Test `tests/triage/test_isolation.py`); Kategorien anderer Nutzer werden nie angeboten. Prompts, Antworten und Begründungen nie in Logs, Fehlercodes statt Exception-Texten. Zurückschreiben aufs Postfach nur nach Opt-in je Postfach |
| LDAP/AD | Nur LDAPS oder StartTLS mit Zertifikats- und Hostnamenprüfung; Klartext nur mit `OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT=true`. Keine leeren Passwörter (Unauthenticated Bind), Filterwerte RFC-4515-escaped, Referrals werden nicht verfolgt. Gespeichert werden nur E-Mail-Adresse, Anzeigename und die Verzeichnis-ID (`objectGUID`/`entryUUID`); Gruppen werden bei jedem Login gelesen, nicht gespeichert. Logs enthalten weder Login-Namen noch DNs ([`auth/ldap.md`](auth/ldap.md)) |
| Anhänge lesen | Textextraktion (PDF, DOCX, TXT, HTML) in einem eigenen Prozess ohne Umgebungsvariablen (keine Secrets), mit Grenzen für Dateigröße, Laufzeit, Speicher und ohne Schreibrechte; Fehler nur als Statuscode |
| Suche/RAG | Zugriff ausschließlich per SQL-Filter auf die lesbaren Postfächer (`app/search/access.py`), getestet in `tests/search/test_service.py` und `tests/rag/` (Nutzer A erfährt nichts aus Mails von Nutzer B, auch nicht mit dessen Postfach als Filter). Mailinhalte stehen im Prompt nur als markierte Daten, die Antwort führt nichts aus; Zitate können nur auf tatsächlich abgerufene Chunks zeigen. Fragen, Antworten und Prompts nie in Logs (nur IDs, Anzahlen, Zeiten wie `ttft_ms`) |
| Single Sign-on (OIDC) | Gespeichert werden nur `sub` (Identität), Gruppen-Claims (für das Rollen-Mapping) und beim ersten Login E-Mail-Adresse und Name; IdP-Tokens nie. Client-Secrets verschlüsselt. `state`/`nonce`/PKCE-Verifier nur im verschlüsselten Einmal-Cookie. Logs nur mit Provider und statischem Fehlercode, nie Claims oder Tokens |
| Login mit GitHub | Gespeichert werden nur die numerische GitHub-Nutzer-ID, die Teams (für das Rollen-Mapping; bei Org-Beschränkung nur Teams der erlaubten Organisationen) und beim ersten Login die verifizierte primäre E-Mail-Adresse und der Name. Das Access-Token wird nur im Callback benutzt, nie gespeichert. Client-Secrets verschlüsselt. Logs nur mit Provider und statischem Fehlercode ([`auth/github.md`](auth/github.md)) |
| Sprachausgabe (TTS) | Lokal (Piper), keine Texte in Logs oder Job-Argumenten; Logs nur mit Stimme, Sprache, Längen und Zeiten. Der Download der Stimmen sendet keine Nutzerdaten |

### Logging im Detail

Umgesetzt in `backend/app/core/logging.py`, abgesichert durch `backend/tests/test_logging.py`:

- Alle Logs (eigene, uvicorn, Bibliotheken) laufen durch eine Pipeline und werden als JSON ausgegeben.
- **PII-Filter:** Felder mit sensiblen Namen werden verworfen, auch verschachtelt – u. a. `subject`,
  `body`, `text`, `content`, `from`, `to`, `cc`, `bcc`, `sender`, `recipient(s)`, `email`, `address`,
  `filename`, `prompt`, `completion`, `messages`, `password`, `token`, `secret`, `cookie`. Ein Feld
  trifft auch, wenn es auf `_<name>` endet (`sender_email`, `refresh_token`, `body_text`).
- **Exceptions:** Typ und Stack-Frames werden geloggt, Exception-Texte und lokale Variablen nie.
- **URLs:** Zugriffslogs enthalten nur das Routen-Template (`/api/messages/{message_id}`), keine
  Query-Strings. URL-Logs von `uvicorn.access`, `httpx` und `httpcore` sind unterdrückt.
- **SQL:** Bind-Parameter werden nie gerendert (`hide_parameters`).
- **API-Fehler:** Validierungsfehler (422) geben die abgelehnten Eingabewerte nicht zurück; 500er
  enthalten nur die Request-ID.
- Der Filter sieht nur Feldnamen, keinen Freitext. Event-Texte sind deshalb statisch
  (`log.info("message_synced", message_id=...)`); Inhalte werden nie hineinformatiert.

### Verschlüsselung von Secrets im Detail

Umgesetzt in `backend/app/core/crypto.py`, abgesichert durch `backend/tests/test_crypto.py`:

- **Envelope-Encryption:** Jedes Secret erhält einen eigenen zufälligen 256-Bit-Data-Key; der Wert
  wird damit per AES-256-GCM verschlüsselt. Der Data-Key wird mit einem aus `OLLAMAIL_SECRET_KEY`
  (HKDF-SHA256) abgeleiteten Key-Encryption-Key verschlüsselt. Bibliothek: `cryptography`.
- **Manipulationsschutz:** GCM-Tags sichern Wert, Data-Key, Version und Key-ID; jede Änderung
  führt zu einem Fehler statt zu falschen Daten.
- **Key-ID im Ciphertext:** Mehrere Master-Keys können parallel gültig sein
  (`OLLAMAIL_SECRET_KEYS_OLD`). `python -m app.cli rotate-keys` verschlüsselt die Data-Keys aller
  Secrets mit dem aktuellen Key neu.
- **Nutzung in Modellen:** Spaltentypen `EncryptedStr` und `EncryptedJSON` ver- und entschlüsseln
  transparent. Neue Secrets **müssen** diese Typen verwenden.
- **Startprüfung:** Ohne oder mit zu schwachem Master-Key (kein Base64, < 32 Bytes, offensichtlich
  nicht zufällig) startet die API nicht. Keys und Klartexte erscheinen nie in Logs oder
  Fehlermeldungen; geloggt wird nur eine nicht umkehrbare Key-ID.

### Audit-Log im Detail

Umgesetzt in `backend/app/audit/`, abgesichert durch `backend/tests/audit/`:

- **Schreiben:** `audit.record(db, actor, action, target, details)` in derselben Transaktion wie die
  protokollierte Änderung. Akteur ist ein Nutzer (ID), `system` (CLI, Jobs) oder `anonymous`
  (fehlgeschlagener Login).
- **Keine Inhalte:** `details` akzeptiert nur flache Werte (IDs, Zähler, Flags, Codes wie
  `reason: locked`). Schlüssel, die der Log-Filter als sensibel einstuft (`email`, `subject`,
  `to`, …), Texte mit `@` oder Zeilenumbruch, lange Texte, Floats und Verschachtelung werden
  abgelehnt. Fehlgeschlagene Logins speichern die eingegebene Adresse **nicht**.
- **Keine Fremdschlüssel:** Nutzer- und Postfach-IDs sind pseudonyme Verweise. Namen werden erst
  beim Lesen ergänzt und verschwinden mit dem Nutzer; Löschen eines Nutzers ändert das Log nicht.
- **Append-only:** Ein Trigger verbietet `UPDATE`, `DELETE` und `TRUNCATE` für alle Rollen.
  Die Aufbewahrungsfrist (`OLLAMAIL_AUDIT_RETENTION_DAYS`, Standard 365 Tage) setzt #36 über
  eine dokumentierte Ausnahme durch.
- **Manipulationserkennung:** Jede Zeile enthält den SHA-256 ihrer Vorgängerin und ihren eigenen
  (`prev_hash`, `hash`). `GET /api/audit/verify` rechnet die Kette nach und meldet die erste
  geänderte oder fehlende Zeile. Grenze: Das Entfernen der *neuesten* Zeilen durch jemanden mit
  direktem Datenbankzugriff erkennt die Kette allein nicht.
- **Zugriff:** Nur Admins (`/api/audit/*`, Admin-Bereich „Audit-Log“: filterbare Liste und
  CSV-Export). Jeder Export wird selbst protokolliert (`audit.exported`). Der CSV-Export
  entschärft Zellen, die mit `=`, `+`, `-` oder `@` beginnen.

| Ereignistyp | Ausgelöst durch | Status |
|---|---|---|
| `auth.setup_completed` | Ersteinrichtung (`POST /api/setup`) | aktiv |
| `auth.login_succeeded`, `auth.login_failed` | Lokaler Login und LDAP (`provider`; Fehlschlag mit `reason`: `invalid_credentials`, `locked`, bei LDAP zusätzlich `user_inactive`, `directory_unavailable`) | aktiv; OIDC mit #30 |
| `auth.logout`, `auth.session_revoked` | Logout, Beenden eigener Sitzungen | aktiv |
| `user.created` | Admin legt Nutzer an, Selbstregistrierung, `app.cli create-admin`, JIT-Provisioning beim ersten LDAP-Login | aktiv |
| `user.role_changed` | Rollen-Sync über LDAP-Gruppen (`admin_groups`) | aktiv; Nutzerverwaltung mit #33 |
| `user.deleted` | Nutzerverwaltung | geplant (#33) |
| `idp.config_changed` | LDAP-Verzeichnis angelegt, geändert, gelöscht (`details.change`) | aktiv; OIDC mit #30 |
| `ai.settings_changed` | KI-Einstellungen inkl. Cloud-Freigabe (`details.cloud_enabled`) | geplant |
| `mailbox.created` | Postfach-API (`POST /api/mailboxes`, `details.type`) | aktiv |
| `mailbox.shared` | Shared Mailboxes | geplant (#34) |
| `mailbox.deleted` | `app.mail.service.delete_mailbox`; über die Postfach-API mit dem Nutzer als Akteur | aktiv |
| `data.exported`, `data.deleted` | Datenexport, Lösch- und Aufbewahrungsjobs | geplant (#36) |
| `crypto.keys_rotated` | `python -m app.cli rotate-keys` (mit Zählern) | aktiv |
| `audit.exported` | CSV-Export des Audit-Logs | aktiv |

## Betroffenenrechte & Löschkonzept

- **Auskunft/Export (Art. 15/20):** Nutzer kann eigene Daten (Triage, Todos, Digests, Chat-Verläufe) exportieren.
- **Löschung (Art. 17):** Postfach entfernen → alle zugehörigen Mails, Anhänge, Embeddings, Todos, Digests
  werden gelöscht (Hard Delete, inkl. Dateien). Nutzer löschen → kaskadierend.
  Umsetzung Mail (`backend/app/mail/service.py`): Alle `mail_*`-Tabellen hängen per
  `ON DELETE CASCADE` am Postfach. Anhänge liegen unter `<OLLAMAIL_DATA_DIR>/attachments/<mailbox_id>/<attachment_id>`
  (Dateinamen ohne Originalnamen); `delete_mailbox` löscht nach dem Commit das ganze Verzeichnis,
  `delete_messages` die Dateien der gelöschten Mails.
  Umsetzung Verarbeitung (`backend/app/processing/`): `message_processing` (Status je Mail und
  Schritt) und `processing_mailbox_settings` (Opt-out je Postfach) hängen per `ON DELETE CASCADE`
  an Mail bzw. Postfach. Gespeichert werden nur Schrittname, Version, Status und ein
  Fehlercode (`StepError.code` oder Name der Exception-Klasse), nie Exception-Texte.
  Umsetzung Triage (`backend/app/triage/`, #20): `triage_results` und `triage_feedback` hängen per
  `ON DELETE CASCADE` an der Mail, `triage_mailbox_settings` am Postfach; eigene Kategorien,
  Sichtbarkeit/Reihenfolge und Absenderregeln am Nutzer. Korrekturen speichern keinen Mailtext,
  sondern verweisen auf die Mail; der Text für Few-Shot-Beispiele wird beim Klassifizieren aus der
  Mail gelesen und verschwindet mit ihr.
  Umsetzung Suchindex (`backend/app/search/`): `search_chunks` (Textabschnitte aus Mails und
  Anhängen) hängen per `ON DELETE CASCADE` an Mail, Anhang und Postfach, `search_embeddings` an
  den Chunks. Mail oder Postfach löschen löscht ihren Index mit. Der Index enthält Mail-Inhalte;
  geloggt werden nur IDs, Anzahlen und Statuscodes.
  Umsetzung Todos (`backend/app/todos/`): `todos` hängt per `ON DELETE CASCADE` an Nutzer und
  Postfach; Quell-Mail und Thread werden beim Löschen einer einzelnen Mail auf `NULL` gesetzt
  (das Todo gehört dem Nutzer und bleibt, bis er es löscht). Die Extraktion protokolliert nur
  Anzahlen, nie Titel oder Beschreibungen. Die API liefert ausschließlich eigene Todos; ein
  fremdes Todo verhält sich wie ein nicht vorhandenes (404).
  Umsetzung Daily Digest (`backend/app/digest/`, #28): `digests` und `digest_user_settings`
  hängen per `ON DELETE CASCADE` am Nutzer. Ein Digest verweist auf Postfächer und Mails nur über
  IDs; der stündliche Job `digest.cleanup` löscht Digests, deren Postfach entfernt wurde, Digests
  nach Ablauf der Aufbewahrungsfrist (`OLLAMAIL_DIGEST_RETENTION_DAYS`, Standard 30 Tage) und
  Audiodateien ohne Digest (z. B. nach dem Löschen eines Nutzers), jeweils inkl. Dateien unter
  `<OLLAMAIL_DATA_DIR>/digests/<user_id>/` (Dateinamen nur aus IDs). Logs enthalten nur IDs,
  Anzahlen und Statuscodes, nie Skript, Betreffzeilen oder Absender. Der Podcast-Feed ist ohne
  Anmeldung erreichbar; Schutz ist allein das Token in der URL (256 Bit, nur als SHA-256-Hash
  gespeichert, widerrufbar, wird im Request-Log nicht protokolliert, da nur das Routen-Template
  geloggt wird). Wer die Feed-URL kennt, kann Skripte und Audio der Digests abrufen; die UI muss
  darauf hinweisen. Der Feed bittet Verzeichnisse per `itunes:block` und `X-Robots-Tag: noindex`,
  ihn nicht aufzunehmen.
  Umsetzung „Frag deine Inbox“ (`backend/app/rag/`, #25): Gespräche (`rag_conversations`)
  hängen per `ON DELETE CASCADE` am Nutzer, Fragen und Antworten (`rag_messages`) am Gespräch.
  Zitierte Ausschnitte (`rag_citations`) hängen zusätzlich per `ON DELETE CASCADE` an Mail,
  Anhang und Postfach: Wird eine Mail gelöscht, verschwindet ihr Ausschnitt aus allen
  Gesprächen. Nutzer löschen einzelne oder alle Gespräche selbst (`DELETE /rag/conversations`);
  der tägliche Job `rag.purge_conversations` löscht Gespräche, in denen seit
  `OLLAMAIL_RAG_HISTORY_RETENTION_DAYS` (Standard 90) keine Frage gestellt wurde.
  Umsetzung API (`DELETE /mailboxes/{id}`, `backend/app/mail/api/`): ruft `delete_mailbox` auf und
  bestätigt die Löschung mit der Anzahl gelöschter Mails und Anhänge. Neue Tabellen anderer
  Module (Triage, Suchindex, …) müssen per `ON DELETE CASCADE` an Postfach oder Mail hängen;
  `tests/mail/api/test_mailbox_deletion.py` ermittelt alle Tabellen mit Bezug zum Postfach aus
  dem Schema und schlägt an, wenn eine davon beim Löschen Zeilen zurücklassen würde.
- **Nutzer löschen:** `users` → `auth_identities` (inkl. gespeicherter Gruppen), `auth_sessions` und eigene Postfächer
  (`mail_mailboxes.owner_user_id`, und damit alle Mail-Daten) per `ON DELETE CASCADE`.
- **Aufbewahrungsfristen:** Pro Instanz konfigurierbar (Mails, Audio-Digests, Chat-Verläufe, Audit-Log).
  Ein periodischer Job setzt sie durch.
- **Mails, die am Server gelöscht wurden**, werden beim nächsten Sync auch lokal gelöscht
  (`app/mail/sync/engine.py`, inkl. Anhangsdateien). Das gilt auch für Ordner, die am Server
  gelöscht wurden, und für Ordner, deren `UIDVALIDITY` sich geändert hat (Neuimport).
- **Mail-Sync:** Importiert werden nur Ordner, deren Rolle nicht ausgeschlossen ist (Standard:
  Papierkorb und Spam werden nicht synchronisiert), und beim Erstimport nur der eingestellte
  Zeitraum. Abrufe ändern keine Flags am Server (`EXAMINE`, `BODY.PEEK`). Sync-Logs und
  `SyncState.last_error` enthalten nur IDs, Zähler und Fehlercodes – keine Ordnernamen,
  Betreffzeilen, Adressen oder Server-Meldungen.
- **Gmail / Google Workspace** (`backend/app/mail/providers/gmail*.py`, Details in
  [`providers/gmail.md`](providers/gmail.md)): Gespeichert wird nur der Refresh-Token
  (verschlüsselt in `mail_mailboxes.credentials`); Access-Tokens liegen nur im Prozessspeicher.
  Angefordert wird nur der Gmail-Scope (`gmail.modify` bzw. mit `OLLAMAIL_GMAIL_READONLY`
  `gmail.readonly`), kein Profil- oder OpenID-Scope. Der OAuth-`state` und der PKCE-Verifier
  liegen in einem signierten, 10 Minuten gültigen `HttpOnly`-Cookie. Fehler enthalten nur Codes,
  nie Antworttexte von Google; Logs nur Nutzer- und Postfach-IDs. Mails nur in Spam/Papierkorb
  werden standardmäßig gar nicht abgerufen. Die Service-Account-Schlüsseldatei für Domain-wide
  Delegation gewährt Zugriff auf alle Postfächer der Domain und ist entsprechend zu schützen
  (Docker-Secret, Scope in der Google Admin Console so eng wie möglich).

## Dokumentation für Betreiber

Betreiber (Unternehmen) benötigen für ihr Verarbeitungsverzeichnis/ihre DSFA:
- Liste der Datenkategorien und Speicherorte (gepflegt in [`OPERATIONS.md`](OPERATIONS.md#9-datenschutz-hinweise-für-betreiber))
- Beschreibung der Datenflüsse inkl. optionaler Cloud-LLMs
- Hinweis zu Betriebsrat/Mitarbeiterüberwachung: ollamail bietet keine Funktionen zur Leistungs- oder
  Verhaltenskontrolle; Admin-Statistiken sind aggregiert und nicht personenbezogen auswertbar.

## Checkliste für jeden PR

- [ ] Keine personenbezogenen Inhalte in Logs, Fehlermeldungen oder Exceptions, die geloggt werden
- [ ] Neue Secrets werden verschlüsselt gespeichert
- [ ] Neue Datentabellen sind im Löschkonzept (kaskadierend) berücksichtigt
- [ ] Zugriffskontrolle serverseitig geprüft (nicht nur im UI)
- [ ] Keine Datenübertragung an Dritte ohne Admin-Opt-in
