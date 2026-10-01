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
| In transit | TLS für IMAP/LDAP/OIDC Pflicht (Ausnahme nur explizit per Admin-Setting), HTTPS hinter Reverse Proxy |
| Logs | **Keine** Betreffzeilen, Adressen, Inhalte, Prompts oder LLM-Antworten in Logs. IDs statt Inhalte. Ein Log-Filter erzwingt das. |
| Job-Queue | Job-Argumente enthalten nur IDs, keine Inhalte. Abgeschlossene Jobs werden nach 7 Tagen gelöscht. Procrastinate-Logs werden auf statische Event-Namen reduziert (keine Argumente, keine Rückgabewerte) |
| Echtzeit-Events | Payload nur Typ, IDs und Status (per Pattern erzwungen); Zustellung ausschließlich an den betroffenen Nutzer |
| Audit-Log | Append-only und hash-verkettet: Login (Erfolg/Fehlschlag), Logout, Setup, Session-Widerruf, Nutzer angelegt, Rollenänderung, IdP- und KI-Einstellungen, Postfach angelegt/entfernt/freigegeben, Export, Löschung, Key-Rotation. Nur IDs und Codes, keine Inhalte (siehe unten) |
| Sessions | Serverseitig, widerrufbar, Lebensdauer und Idle-Timeout konfigurierbar. In der DB nur der SHA-256 des Cookie-Tokens; Cookies `HttpOnly`, `Secure`, `SameSite=Lax`; CSRF-Schutz per signiertem Double-Submit-Token |
| Passwörter | Argon2id (RFC 9106); Rate-Limit und Kontosperre in Postgres. Die Zähler speichern nur HMACs von IP-Adresse bzw. E-Mail-Adresse und werden stündlich bereinigt |
| Telemetrie | Keine. Keine externen Fonts/CDNs im Frontend. Die eingebaute Telemetrie von ONNX Runtime (von Piper genutzt) ist per `ORT_DISABLE_TELEMETRY=1` abgeschaltet, im Code und im Image (Test: `tests/ai/tts/test_piper.py`) |
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
| `auth.login_succeeded`, `auth.login_failed` | Lokaler Login (Fehlschlag mit `reason`: `invalid_credentials`, `locked`) | aktiv; OIDC/LDAP mit #30, #32 |
| `auth.logout`, `auth.session_revoked` | Logout, Beenden eigener Sitzungen | aktiv |
| `user.created` | Admin legt Nutzer an, Selbstregistrierung, `app.cli create-admin` | aktiv |
| `user.role_changed`, `user.deleted` | Nutzerverwaltung | geplant (#33) |
| `idp.config_changed` | IdP-/LDAP-Konfiguration | geplant (#30, #32) |
| `ai.settings_changed` | KI-Einstellungen inkl. Cloud-Freigabe (`details.cloud_enabled`) | geplant |
| `mailbox.created`, `mailbox.shared` | Postfach-API, Shared Mailboxes | geplant (#15) |
| `mailbox.deleted` | `app.mail.service.delete_mailbox` | aktiv |
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
- **Nutzer löschen:** `users` → `auth_identities`, `auth_sessions` und eigene Postfächer
  (`mail_mailboxes.owner_user_id`, und damit alle Mail-Daten) per `ON DELETE CASCADE`.
- **Aufbewahrungsfristen:** Pro Instanz konfigurierbar (Mails, Audio-Digests, Chat-Verläufe, Audit-Log).
  Ein periodischer Job setzt sie durch.
- **Mails, die am Server gelöscht wurden**, werden beim nächsten Sync auch lokal gelöscht.

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
