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
| Audit-Log | Login, Rollenänderung, IdP-Konfiguration, Postfach hinzugefügt/entfernt, Export, Löschung |
| Sessions | Serverseitig, widerrufbar, Ablaufzeit konfigurierbar |
| Telemetrie | Keine. Keine externen Fonts/CDNs im Frontend. |

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

## Betroffenenrechte & Löschkonzept

- **Auskunft/Export (Art. 15/20):** Nutzer kann eigene Daten (Triage, Todos, Digests, Chat-Verläufe) exportieren.
- **Löschung (Art. 17):** Postfach entfernen → alle zugehörigen Mails, Anhänge, Embeddings, Todos, Digests
  werden gelöscht (Hard Delete, inkl. Dateien). Nutzer löschen → kaskadierend.
  Umsetzung Mail (`backend/app/mail/service.py`): Alle `mail_*`-Tabellen hängen per
  `ON DELETE CASCADE` am Postfach. Anhänge liegen unter `<OLLAMAIL_DATA_DIR>/attachments/<mailbox_id>/<attachment_id>`
  (Dateinamen ohne Originalnamen); `delete_mailbox` löscht nach dem Commit das ganze Verzeichnis,
  `delete_messages` die Dateien der gelöschten Mails.
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
