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
   nur Metadaten (Anzahl, Sync-Status, Fehler) und aggregierte Statistiken. Die Nutzerverwaltung
   (Admin → Nutzer) zeigt nur Kontodaten: Name, Adresse, Rolle, Anmeldeverfahren, Status, letzte
   Anmeldung und Zahl der Sitzungen. Auch Shared Mailboxes verwaltet der Admin nur (Verbindung,
   Ordner, Zuweisungen, Sync-Status); lesen kann er sie nur, wenn er sich selbst zuweist, und
   das steht im Audit-Log.
5. **Transparenz** – Jede KI-Bewertung (Triage, Todo) ist für den Nutzer erklärbar und korrigierbar.

## Technische Maßnahmen

| Thema | Maßnahme |
|---|---|
| Secrets | IMAP-Passwörter, OAuth-Tokens, IdP-Client-Secrets, LDAP-Bind-Passwörter: AES-256-GCM, Envelope-Encryption mit Master-Key aus `OLLAMAIL_SECRET_KEY` (Key-Rotation unterstützt) |
| At rest | Empfehlung: verschlüsseltes Volume/Dateisystem. Optional: Verschlüsselung von Mail-Bodies/Anhängen auf Anwendungsebene (Feature-Flag) |
| Microsoft 365 | OAuth-Tokens verschlüsselt (`mail_mailboxes.credentials`), Client-Secret nur in der Umgebung. Zum Senden wird beim Verbinden `Mail.Send` angefordert (abschaltbar mit `OLLAMAIL_MAIL_GRAPH_SEND_ENABLED=false`). App-only-Zugriff nur mit Einschränkung auf freigegebene Postfächer (RBAC for Applications / `ApplicationAccessPolicy`, siehe `docs/providers/microsoft365.md`). Change Notifications optional, ohne Inhalte (nur IDs, `clientState` per HMAC geprüft) |
| In transit | TLS für IMAP/SMTP/LDAP/OIDC Pflicht (Ausnahme nur explizit per Admin-Setting, IMAP und SMTP: `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS`), HTTPS hinter Reverse Proxy |
| Logs | **Keine** Betreffzeilen, Adressen, Inhalte, Prompts oder LLM-Antworten in Logs. IDs statt Inhalte. Ein Log-Filter erzwingt das. |
| Job-Queue | Job-Argumente enthalten nur IDs, keine Inhalte. Abgeschlossene Jobs werden nach 7 Tagen gelöscht. Procrastinate-Logs werden auf statische Event-Namen reduziert (keine Argumente, keine Rückgabewerte) |
| Echtzeit-Events | Payload nur Typ, IDs und Status (per Pattern erzwungen); Zustellung ausschließlich an den betroffenen Nutzer |
| Audit-Log | Append-only und hash-verkettet: Login (Erfolg/Fehlschlag), Logout, Setup, Session-Widerruf, Nutzer angelegt/geändert (SCIM), Rollenänderung, SCIM-Gruppen und -Mitgliedschaften, IdP- und KI-Einstellungen, Postfach angelegt/entfernt/freigegeben, Mail gesendet, Export, Löschung, Key-Rotation. Nur IDs und Codes, keine Inhalte (siehe unten) |
| Sessions | Serverseitig, widerrufbar, Lebensdauer und Idle-Timeout konfigurierbar. In der DB nur der SHA-256 des Cookie-Tokens; Cookies `HttpOnly`, `Secure`, `SameSite=Lax`; CSRF-Schutz per signiertem Double-Submit-Token |
| Passwörter | Argon2id (RFC 9106); Rate-Limit und Kontosperre in Postgres. Die Zähler speichern nur HMACs von IP-Adresse bzw. E-Mail-Adresse und werden stündlich bereinigt |
| Telemetrie | Keine. Keine externen Fonts/CDNs im Frontend. Die eingebaute Telemetrie von ONNX Runtime (von Piper genutzt) ist per `ORT_DISABLE_TELEMETRY=1` abgeschaltet, im Code und im Image (Test: `tests/ai/tts/test_piper.py`) |
| Triage | Few-Shot-Beispiele nur aus Korrekturen desselben Nutzers in eigenen Postfächern (doppelt gefiltert, Test `tests/triage/test_isolation.py`); Kategorien anderer Nutzer werden nie angeboten. Ausnahme Shared Mailboxes: Korrekturen wirken postfachweit und sind Beispiele nur für dieses Postfach, das alle Beteiligten ohnehin lesen dürfen; persönliche Korrekturen fließen dort nie ein. Prompts, Antworten und Begründungen nie in Logs, Fehlercodes statt Exception-Texten. Zurückschreiben aufs Postfach nur nach Opt-in je Postfach |
| LDAP/AD | Nur LDAPS oder StartTLS mit Zertifikats- und Hostnamenprüfung; Klartext nur mit `OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT=true`. Keine leeren Passwörter (Unauthenticated Bind), Filterwerte RFC-4515-escaped, Referrals werden nicht verfolgt. Gespeichert werden nur E-Mail-Adresse, Anzeigename, die Verzeichnis-ID (`objectGUID`/`entryUUID`) und die Gruppen-DNs des letzten Logins (`auth_identities.groups`, für Rollen-Mapping und Gruppenzuweisungen von Shared Mailboxes; mit dem Nutzer gelöscht). Logs enthalten weder Login-Namen noch DNs ([`auth/ldap.md`](auth/ldap.md)) |
| Mails anzeigen | HTML serverseitig sanitisiert (`nh3`), im Browser zusätzlich in einem sandboxed `iframe` ohne Skripte mit eigener CSP. Externe Bilder (Tracking-Pixel) sind blockiert, bis der Nutzer sie für eine Mail lädt; dann ohne Referrer. Anhänge nur als Download (`application/octet-stream`, `nosniff`, CSP `sandbox`), inline nur Rasterbilder für `cid:`. Gelesen/ungelesen geht nur nach ausdrücklicher Aktion des Nutzers (Öffnen, `u`) an den Mailserver |
| Anhänge lesen | Textextraktion (PDF, DOCX, TXT, HTML) in einem eigenen Prozess ohne Umgebungsvariablen (keine Secrets), mit Grenzen für Dateigröße, Laufzeit, Speicher und ohne Schreibrechte; Fehler nur als Statuscode |
| Zugriff auf Postfächer | Eine einzige Regel für alle Features: `accessible_mailbox_ids(user)` in `app/mail/access.py` (eigene Postfächer und zugewiesene Shared Mailboxes), immer als SQL-Filter, ohne Cache. Ein Entzug wirkt mit der nächsten Anfrage in Inbox, Thread, Anhängen, Triage, Todos, Suche, RAG (auch gespeicherte Antworten und Zitate), Digest (auch gespeicherte Digests und Podcast-Feed) und Antwortentwürfen; getestet je Feature in `tests/shared/test_access.py` (Entwürfe: `tests/drafts/test_api.py`). Nutzer eines Shared Mailbox haben nur Leserecht |
| Suche/RAG | Zugriff ausschließlich per SQL-Filter auf die lesbaren Postfächer (`app/mail/access.py`), getestet in `tests/search/test_service.py`, `tests/rag/` und `tests/shared/` (Nutzer A erfährt nichts aus Mails von Nutzer B, auch nicht mit dessen Postfach als Filter). Mailinhalte stehen im Prompt nur als markierte Daten, die Antwort führt nichts aus; Zitate können nur auf tatsächlich abgerufene Chunks zeigen. Fragen, Antworten und Prompts nie in Logs (nur IDs, Anzahlen, Zeiten wie `ttft_ms`) |
| Single Sign-on (OIDC) | Gespeichert werden nur `sub` (Identität), Gruppen-Claims (für das Rollen-Mapping) und beim ersten Login E-Mail-Adresse und Name; IdP-Tokens nie. Client-Secrets verschlüsselt. `state`/`nonce`/PKCE-Verifier nur im verschlüsselten Einmal-Cookie. Logs nur mit Provider und statischem Fehlercode, nie Claims oder Tokens |
| Login mit GitHub | Gespeichert werden nur die numerische GitHub-Nutzer-ID, die Teams (für das Rollen-Mapping; bei Org-Beschränkung nur Teams der erlaubten Organisationen) und beim ersten Login die verifizierte primäre E-Mail-Adresse und der Name. Das Access-Token wird nur im Callback benutzt, nie gespeichert. Client-Secrets verschlüsselt. Logs nur mit Provider und statischem Fehlercode ([`auth/github.md`](auth/github.md)) |
| Login mit SAML | Gespeichert werden nur die Kennung (NameID bzw. konfiguriertes Attribut), die Gruppen aus dem Gruppen-Attribut (für das Rollen-Mapping) und beim ersten Login E-Mail-Adresse und Anzeigename; weitere Attribute werden verworfen. Gegen Replay nur SHA-256 von Provider und Assertion-ID (`auth_saml_assertions`, gelöscht nach Ablauf, höchstens 24 h). Keine Secrets (IdP-Zertifikate sind öffentlich). Logs nur mit Provider, Code und statischem Prüfschritt, nie Attribute oder die Response ([`auth/saml.md`](auth/saml.md)) |
| Antwortentwürfe und Versand | Entwürfe erzeugt das Modell der Aufgabe `reply_draft` (lokal, solange der Admin keinen Cloud-Provider zuweist; dann erscheint die Aufgabe in der Cloud-Anzeige). Kontext nur aus Mails, die der Nutzer lesen darf (SQL-Filter); Stilbeispiele nur aus den eigenen gesendeten Mails eigener Postfächer, pro Nutzer abschaltbar. Mailinhalte stehen nur als markierte Daten im Prompt. **Nichts wird automatisch gesendet**: Senden ist ein eigener Request des Autors, nur aus eigenen Postfächern (Shared Mailboxes: nur lesen). Empfänger kommen aus den Kopfzeilen, nie vom Modell; Kopfzeilen werden gegen Header-Injection geprüft. Jeder Versand steht im Audit-Log (`mail.sent`, nur IDs und Anzahl Empfänger). Entwürfe, Prompts, Anweisungen und Antworten nie in Logs (nur IDs, Anzahlen, Zeiten, Fehlercodes); SMTP-Serverantworten werden weder geloggt noch weitergegeben |
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

### Cloud-LLMs im Detail

- Standard: aus. Der Admin erlaubt Cloud-LLMs im Admin-Bereich „KI“ ausdrücklich; vor dem
  Einschalten bestätigt er einen Hinweis zur Datenübermittlung. Jede Änderung steht im Audit-Log.
- Ein Provider gilt als Cloud-Provider, wenn er als „Cloud“ markiert ist. Solange Cloud-LLMs nicht
  erlaubt sind, lehnt das Gateway jede Anfrage an ihn ab, bevor eine Verbindung entsteht.
  Ist die Datenbank nicht lesbar, bleibt die Cloud gesperrt (fail closed).
- Der Verbindungstest im Admin-Bereich ruft nur die Modellliste ab; es gehen keine Mail-Inhalte hinaus.
- Für alle Nutzer zeigt die UI dauerhaft und dezent an, welcher Cloud-Provider für welche Aufgaben
  (Triage, Aufgaben, Zusammenfassung, Fragen, Suchindex) Mail-Inhalte erhält (`GET /api/ai/status`).
- API-Keys werden verschlüsselt gespeichert (`EncryptedStr`) und nie an das Frontend zurückgegeben
  (nur „gesetzt/nicht gesetzt“).

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
  Einzige Ausnahme ist die Aufbewahrungsfrist (Admin → Aufbewahrung, sonst
  `OLLAMAIL_AUDIT_RETENTION_DAYS`, Standard 365 Tage), siehe „Aufbewahrung des Audit-Logs“.
- **Manipulationserkennung:** Jede Zeile enthält den SHA-256 ihrer Vorgängerin und ihren eigenen
  (`prev_hash`, `hash`). `GET /api/audit/verify` rechnet die Kette nach und meldet die erste
  geänderte oder fehlende Zeile. Grenze: Das Entfernen der *neuesten* Zeilen durch jemanden mit
  direktem Datenbankzugriff erkennt die Kette allein nicht.
- **Aufbewahrung des Audit-Logs (dokumentierte Ausnahme):** Der tägliche Job `privacy.retention`
  ruft die Datenbankfunktion `audit_events_purge(cutoff)` auf (Migration `add_privacy`). Sie
  nimmt die Sperre der Hash-Kette, bestimmt die erste zu behaltende Zeile (die älteste ab
  `cutoff`, höchstens die neueste) und trägt deren ID in die **transaktionslokale** Einstellung
  `ollamail.audit_purge_below` ein. Nur für `DELETE`s von Zeilen mit kleinerer ID lässt der
  Trigger eine Ausnahme zu; danach wird die Einstellung sofort geleert. `UPDATE`, `TRUNCATE` und
  jedes andere `DELETE` bleiben verboten (Tests: `tests/privacy/test_retention.py`). Gelöscht
  wird immer ein zusammenhängender Block der ältesten Zeilen, die neueste Zeile nie.
- **Prüfbarkeit nach der Aufbewahrung:** `GET /api/audit/verify` prüft die Kette ab der ältesten
  verbliebenen Zeile; deren `prev_hash` zeigt auf die gelöschte Vorgängerin. Den neuen
  Startpunkt dokumentiert der Job im eigenen, selbst verketteten `data.deleted`-Eintrag
  (`details.chain_start_id`, `details.chain_start_prev_hash`, `details.audit_events`). Ein
  Prüfer vergleicht die erste verbliebene Zeile mit dem jüngsten solchen Eintrag. Grenze: Wer
  direkten Datenbankzugriff hat, kann die Einstellung selbst setzen, so wie er auch den Trigger
  entfernen kann. Der Schutz richtet sich gegen Fehler und Missbrauch über die Anwendung, nicht
  gegen Datenbank-Administratoren.
- **Zugriff:** Nur Admins (`/api/audit/*`, Admin-Bereich „Audit-Log“: filterbare Liste und
  CSV-Export). Jeder Export wird selbst protokolliert (`audit.exported`). Der CSV-Export
  entschärft Zellen, die mit `=`, `+`, `-` oder `@` beginnen.

| Ereignistyp | Ausgelöst durch | Status |
|---|---|---|
| `auth.setup_completed` | Ersteinrichtung (`POST /api/setup`) | aktiv |
| `auth.login_succeeded`, `auth.login_failed` | Lokaler Login und LDAP (`provider`; Fehlschlag mit `reason`: `invalid_credentials`, `locked`, bei LDAP zusätzlich `user_inactive`, `directory_unavailable`) | aktiv; OIDC mit #30 |
| `auth.logout`, `auth.session_revoked` | Logout, Beenden eigener Sitzungen | aktiv |
| `user.created` | Admin legt Nutzer an oder lädt ein (`via: invitation`), Selbstregistrierung, `app.cli create-admin`, JIT-Provisioning beim ersten externen Login | aktiv |
| `user.role_changed` | Nutzerverwaltung (`via: admin`), Rollen-Zuordnung bzw. LDAP-`admin_groups` beim Login (Akteur `system`, `provider`) | aktiv |
| `user.deactivated`, `user.reactivated` | Nutzerverwaltung (Deaktivieren beendet alle Sitzungen, `details.sessions`); `app.cli reset-password --activate` | aktiv |
| `user.invited` | Einladung bzw. neuer Einladungslink (`renewed`) | aktiv |
| `user.password_set` | Einladung angenommen (`via: invitation`), `app.cli reset-password` (`via: cli`) | aktiv |
| `user.deleted` | Konto löschen (`DELETE /api/privacy/account`, `details.via: self`) und Nutzer löschen durch Admins (`DELETE /api/admin/privacy/users/{id}`, `via: admin`); `details.mailboxes` = Anzahl gelöschter Postfächer | aktiv |
| `idp.config_changed` | LDAP-Verzeichnis bzw. OIDC-Provider angelegt, geändert, gelöscht (`details.change`); lokale Anmeldung an/aus (`kind: local`); Rollen-Zuordnung gespeichert (`kind: role_mapping`, nur Anzahlen) | aktiv |
| `ai.settings_changed` | KI-Einstellungen im Admin-Bereich: Provider anlegen/ändern/löschen (`details.change`, `provider`, `is_cloud`), Modell-Zuordnung, Profil, Parallelität, Cloud-Freigabe (`details.cloud_enabled`) | aktiv |
| `mailbox.created` | Postfach-API (`POST /api/mailboxes`, `details.type`) | aktiv |
| `mailbox.shared`, `mailbox.unshared` | Admin weist ein Shared Mailbox einem Nutzer oder einer Gruppe zu bzw. entzieht es (`/api/admin/shared-mailboxes/{id}/assignments`; je Eintrag `principal`, Nutzer-ID bzw. Gruppenname – nur wenn kurz und ohne `@`, sonst die Zuweisungs-ID – und `provider`); Anlegen eines Shared Mailbox als `mailbox.created` mit `shared: true` | aktiv |
| `mailbox.deleted` | `app.mail.service.delete_mailbox`; über die Postfach-API mit dem Nutzer als Akteur | aktiv |
| `mail.sent` | Antwort gesendet (`POST /api/drafts/{id}/send`): Ziel ist das Postfach; `details` nur `draft_id`, `message_id` (beantwortete Mail), `reply_all`, `recipient_count`, `refused` (abgelehnte Empfänger) und `sent_copy` (Kopie in „Gesendet“ abgelegt) | aktiv |
| `data.exported` | Datenexport: angefordert und heruntergeladen (`details.stage`: `requested`, `downloaded`; `export_id`) | aktiv |
| `data.deleted` | Aufbewahrungsjob `privacy.retention`, nur wenn er etwas gelöscht hat: Anzahlen (`mails`, `attachments`, `search_chunks`, `threads`, `audit_events`) und neuer Startpunkt der Hash-Kette | aktiv |
| `data.retention_changed` | Admin → Aufbewahrung: geänderte Fristen in Tagen (`mail_days`, …) | aktiv |
| `crypto.keys_rotated` | `python -m app.cli rotate-keys` (mit Zählern) | aktiv |
| `audit.exported` | CSV-Export des Audit-Logs | aktiv |

## Betroffenenrechte & Löschkonzept

- **Auskunft/Export (Art. 15/20):** Unter Einstellungen → Deine Daten fordert der Nutzer einen
  Export an (`POST /api/privacy/exports`). Der Hintergrundjob `privacy.export` (Argument: nur die
  Export-ID) schreibt ein ZIP mit JSON-Dateien: Profil mit Anmeldeidentitäten und Sitzungen,
  eigene Postfächer (ohne Zugangsdaten), eigene Kategorien, Kategorie-Einstellungen,
  Absenderregeln, Korrekturen und die Triage-Ergebnisse der eigenen Mails, Aufgaben,
  Digest-Einstellungen (ohne Feed-Token) und Digests mit Audiodateien, Fragen-Verläufe mit
  Zitaten, Antwortentwürfe mit Signatur und Einstellungen (`app/privacy/export.py`,
  `manifest.json` listet den Inhalt). Mails selbst sind nicht
  enthalten; sie liegen beim Mail-Anbieter. Jede Abfrage filtert auf den Nutzer bzw. auf
  Postfächer, deren Eigentümer er ist; `tests/privacy/test_export.py` prüft, dass keine IDs,
  Texte oder Dateien anderer Nutzer im ZIP stehen. Der Download (`GET
  /api/privacy/exports/{id}/download`) ist nur mit der Session des Eigentümers möglich (sonst
  404) und nur bis `expires_at` (`OLLAMAIL_PRIVACY_EXPORT_EXPIRY_HOURS`, Standard 24 Stunden);
  danach löscht der stündliche Job `privacy.cleanup_exports` Zeile und Datei. Der Nutzer kann
  einen Export auch vorher löschen. Anforderung und Download stehen im Audit-Log
  (`data.exported`).
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
  Anzahlen, nie Titel oder Beschreibungen. Die API liefert ausschließlich eigene Todos und die
  Team-Todos lesbarer Shared Mailboxes; ein fremdes Todo verhält sich wie ein nicht vorhandenes
  (404). Team-Todos (`user_id IS NULL`) hängen am Shared Mailbox; die Zuweisung an eine Person
  (`assignee_id`) wird beim Löschen dieser Person auf `NULL` gesetzt.
  Umsetzung Shared Mailboxes (`backend/app/mail/`, #34): `mail_mailbox_assignments` hängt per
  `ON DELETE CASCADE` am Postfach und am Nutzer. Wird ein Nutzer gelöscht, verschwinden nur seine
  Zuweisungen, das Shared Mailbox und seine Daten bleiben für die anderen erhalten.
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
  Umsetzung Antwortentwürfe (`backend/app/drafts/`, #92): `reply_drafts` hängt per
  `ON DELETE CASCADE` an Nutzer und Postfach; beantwortete Mail und Thread werden beim Löschen
  auf `NULL` gesetzt (der Entwurf bleibt sichtbar, kann aber nicht mehr gesendet werden).
  `reply_draft_settings` (Signatur, Stilbeispiele) hängt am Nutzer. Der tägliche Job
  `drafts.purge` löscht Entwürfe – gesendete, verworfene und offene –, die seit
  `OLLAMAIL_DRAFTS_RETENTION_DAYS` (Standard 30) nicht geändert wurden; Nutzer löschen einzelne
  Entwürfe selbst (`DELETE /drafts/{id}`). Gesendete Mails selbst liegen beim Mail-Anbieter
  (Ordner „Gesendet“) und kommen per Sync wie jede andere Mail in `mail_messages`.
  Umsetzung API (`DELETE /mailboxes/{id}`, `backend/app/mail/api/`): ruft `delete_mailbox` auf und
  bestätigt die Löschung mit der Anzahl gelöschter Mails und Anhänge. Neue Tabellen anderer
  Module (Triage, Suchindex, …) müssen per `ON DELETE CASCADE` an Postfach oder Mail hängen;
  `tests/mail/api/test_mailbox_deletion.py` ermittelt alle Tabellen mit Bezug zum Postfach aus
  dem Schema und schlägt an, wenn eine davon beim Löschen Zeilen zurücklassen würde.
- **Nutzer löschen (Art. 17):** Der Nutzer löscht sein Konto unter Einstellungen → Deine Daten
  (`DELETE /api/privacy/account`, Bestätigung durch Eingabe der eigenen E-Mail-Adresse; per
  `OLLAMAIL_PRIVACY_SELF_DELETE_ENABLED=false` abschaltbar), ein Admin löscht beliebige Nutzer
  (`DELETE /api/admin/privacy/users/{id}`). Der letzte aktive Admin kann nicht gelöscht werden
  (409), ebenso kein Admin, ohne den kein Admin mit funktionierender Anmeldung bliebe
  (`admin-lockout`, `app/auth/admin_access.py`). `app/privacy/deletion.py` löscht die Zeile in `users`; alle Tabellen mit Nutzerbezug
  hängen direkt oder über Postfach, Mail, Gespräch usw. per `ON DELETE CASCADE` daran (Liste
  unten). Nach dem Commit werden die Dateien entfernt: Anhänge je Postfach, Digest-Audio und
  Exporte je Nutzer. Sitzungen enden sofort. Nachweis: ein `user.deleted`-Eintrag nur mit IDs und
  der Anzahl der Postfächer; Name und Adresse verschwinden mit der Nutzerzeile auch aus der
  Anzeige des Audit-Logs. `tests/privacy/test_user_deletion.py` ermittelt alle Tabellen mit
  Bezug auf `users` aus den Fremdschlüsseln des Schemas, füllt jede davon für den gelöschten
  Nutzer und prüft, dass danach keine Zeile übrig bleibt, dass die Dateien weg sind und dass ein
  zweiter Nutzer unverändert bleibt. Ein weiterer Test schlägt an, wenn eine Spalte `user_id`
  bzw. `*_user_id` ohne Fremdschlüssel angelegt wird (Ausnahme: `audit_events.actor_id`).
- **Aufbewahrungsfristen:** Instanzweit unter Admin → Aufbewahrung (`/api/admin/privacy/retention`,
  Tabelle `privacy_retention_settings`; Felder ohne Wert folgen der Umgebung). Fristen in Tagen,
  0 = unbegrenzt:

  | Kategorie | Standard (Umgebung) | Durchgesetzt von | Alter gemessen an |
  |---|---|---|---|
  | Mails inkl. Anhängen, Suchindex, Triage-Ergebnissen, Zitaten | `OLLAMAIL_PRIVACY_MAIL_RETENTION_DAYS` = 0 | `privacy.retention`, täglich | Empfangsdatum, sonst Sendedatum, sonst Importzeitpunkt |
  | Anhänge (nur Dateien, die Mail bleibt) | `OLLAMAIL_PRIVACY_ATTACHMENT_RETENTION_DAYS` = 0 | `privacy.retention` | wie Mails |
  | Suchindex (Abschnitte und Embeddings) | `OLLAMAIL_PRIVACY_SEARCH_INDEX_RETENTION_DAYS` = 0 | `privacy.retention` | wie Mails |
  | Fragen-Verläufe | `OLLAMAIL_RAG_HISTORY_RETENTION_DAYS` = 90 | `rag.purge_conversations`, täglich | letzte Frage |
  | Antwortentwürfe (Umgebung, kein Admin-Feld) | `OLLAMAIL_DRAFTS_RETENTION_DAYS` = 30 | `drafts.purge`, täglich | letzte Änderung |
  | Digests inkl. Audio (mindestens 1 Tag) | `OLLAMAIL_DIGEST_RETENTION_DAYS` = 30 | `digest.cleanup`, stündlich | Erstellung |
  | Audit-Log | `OLLAMAIL_AUDIT_RETENTION_DAYS` = 365 | `privacy.retention` | Ereigniszeitpunkt |
  | Datenexporte | `OLLAMAIL_PRIVACY_EXPORT_EXPIRY_HOURS` = 24 (Stunden) | `privacy.cleanup_exports`, stündlich | Fertigstellung |

  Gelöscht wird hart, in Stapeln, Dateien nach jedem Commit. Threads ohne verbliebene Mail
  verschwinden mit. Eine Mail-Frist kürzer als der Erstimport (`OLLAMAIL_MAIL_INITIAL_SYNC_DAYS`)
  führt dazu, dass ein vollständiger Neuabgleich ältere Mails importiert, die in der nächsten
  Nacht wieder gelöscht werden; die Admin-Seite weist darauf hin. Jede Änderung der Fristen
  steht im Audit-Log (`data.retention_changed`), jeder Lauf mit Löschungen als `data.deleted`
  mit Anzahlen.
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
  `gmail.readonly`), kein Profil- oder OpenID-Scope. `gmail.modify` erlaubt auch das Senden
  von Antworten (#92); gesendet wird nur auf Anforderung des Nutzers. Der OAuth-`state` und der PKCE-Verifier
  liegen in einem signierten, 10 Minuten gültigen `HttpOnly`-Cookie. Fehler enthalten nur Codes,
  nie Antworttexte von Google; Logs nur Nutzer- und Postfach-IDs. Mails nur in Spam/Papierkorb
  werden standardmäßig gar nicht abgerufen. Die Service-Account-Schlüsseldatei für Domain-wide
  Delegation gewährt Zugriff auf alle Postfächer der Domain und ist entsprechend zu schützen
  (Docker-Secret, Scope in der Google Admin Console so eng wie möglich).

## Tabellen und Speicherorte (Grundlage für das Verarbeitungsverzeichnis)

Stand dieser Version. „Löschung“ nennt, wodurch eine Zeile verschwindet: Kaskade beim Löschen
von Nutzer (U), Postfach (P), Mail (M), Anhang (A) oder Gespräch (G), oder ein Job.

| Tabelle / Ort | Inhalt (personenbezogen) | Löschung |
|---|---|---|
| `users` | E-Mail-Adresse, Anzeigename, Rolle, Sprache, Zeitzone, letzter Login | Konto löschen |
| `auth_identities` | Anbieter, Kennung beim Anbieter (`sub`, GitHub-ID, LDAP-GUID, SAML-NameID), Gruppen, Argon2id-Hash | U |
| `auth_sessions` | SHA-256 des Session-Tokens, gekürzte Browser-Kennung, Zeiten | U; abgelaufene stündlich (`auth.cleanup`) |
| `scim_users`, `scim_group_members` | `userName` und `externalId` beim IdP, Gruppenmitgliedschaften | U |
| `scim_groups` | Gruppenname und `externalId` (nicht personenbezogen) | per SCIM; Admin |
| `scim_tokens`, `scim_config` | SHA-256 und Präfix der SCIM-Tokens, Schalter (nicht personenbezogen) | Admin (widerrufen) |
| `auth_rate_limits` | HMAC von IP bzw. E-Mail-Adresse, Zähler | stündlich (`auth.cleanup`) |
| `auth_saml_assertions` | SHA-256 von SAML-Provider und Assertion-ID (Replay-Schutz; nicht personenbezogen) | abgelaufene (Gültigkeit höchstens 24 h) beim nächsten SAML-Login |
| `mail_mailboxes` | Postfachadresse, Anzeigename, Servereinstellungen, Zugangsdaten (verschlüsselt) | U; Postfach entfernen |
| `mail_folders`, `mail_sync_states` | Ordnernamen, Sync-Cursor, Fehlercodes | P |
| `mail_threads` | Betreff-Schlüssel, Zeitpunkt der letzten Mail | P; leer nach Aufbewahrung (`privacy.retention`) |
| `mail_messages`, `mail_message_folders` | Header, Adressen, Betreff, Text, HTML, Flags | P; Aufbewahrung Mails; am Server gelöscht (Sync) |
| `mail_attachments` + `<data>/attachments/<mailbox_id>/<attachment_id>` | Dateiname, Typ, Größe, Inhalt (Datei) | M, P; Aufbewahrung Anhänge |
| `message_processing`, `processing_mailbox_settings` | Schritt, Status, Fehlercode; Opt-out je Postfach | M bzw. P |
| `search_chunks`, `search_embeddings` | Textabschnitte aus Mails und Anhängen, Vektoren | M, A, P; Aufbewahrung Suchindex |
| `search_index_state` | aktives Embedding-Modell (nicht personenbezogen) | – |
| `triage_results` | Kategorie, Priorität, Begründung je Mail | M |
| `triage_feedback` | Korrekturen (Mail-ID, Kategorie, Priorität, Embedding) | M, U |
| `triage_categories`, `triage_category_preferences`, `triage_sender_rules` | eigene Kategorien, Reihenfolge/Sichtbarkeit, Absenderadressen bzw. Domains | U (Organisationskategorien: Admin) |
| `triage_mailbox_settings` | Zurückschreiben je Postfach | P |
| `todos` | Titel, Beschreibung, Fälligkeit, Status; Verweis auf Mail | U, P (Mail-Verweis wird bei M geleert) |
| `digest_user_settings` | Zeitplan, Stimme, Postfachauswahl, SHA-256 des Feed-Tokens | U |
| `digests` + `<data>/digests/<user_id>/<digest_id>.{mp3,opus}` | Skript, Titel, Verweise auf Mails, Audio | U; Aufbewahrung Digests; Postfach entfernt (`digest.cleanup`) |
| `rag_conversations`, `rag_messages` | Fragen und Antworten | U; Nutzer; Aufbewahrung Fragen-Verläufe |
| `rag_citations` | zitierte Ausschnitte aus Mails | G, M, A, P |
| `reply_drafts` | Antwortentwürfe: Empfänger, Betreff, Text, Anweisung, Status, Versandzeitpunkt; Verweis auf Mail und Thread | U, P (Mail-/Thread-Verweis wird bei M geleert); Aufbewahrung Entwürfe; Nutzer |
| `reply_draft_settings` | Signatur, Stilbeispiele an/aus | U |
| `privacy_exports` + `<data>/exports/<user_id>/<export_id>.zip` | Status, Größe, Ablaufzeit; ZIP mit allen Daten des Nutzers | U; Ablauf (`privacy.cleanup_exports`); Nutzer |
| `privacy_retention_settings` | Fristen, Zähler des letzten Laufs (nicht personenbezogen) | – |
| `audit_events` | Ereignis, Zeitpunkt, Nutzer- bzw. Objekt-ID (pseudonym, ohne Fremdschlüssel), Codes, Zähler | Aufbewahrung Audit-Log (dokumentierte Ausnahme) |
| `ai_settings`, `ai_providers`, `auth_oidc_providers`, `auth_github_providers`, `auth_saml_providers`, `auth_ldap_directories` | Instanzkonfiguration, Secrets verschlüsselt | Admin |
| `procrastinate_jobs`, `procrastinate_events` | Job-Argumente (nur IDs) | nach 7 Tagen (`worker.remove_old_jobs`) |
| `<data>/tts/voices/` | Sprachmodelle (nicht personenbezogen) | – |
| Logs (stdout) | IDs, Codes, Anzahlen, Zeiten; keine Inhalte | Log-Rotation des Hosts |

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
