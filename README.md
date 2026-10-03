# ollamail

Selbst gehostetes, „local first“ E-Mail-Analyse-Tool – für Einzelpersonen und Unternehmen.

- **Triage** eingehender E-Mails (anpassbare Kategorien, lernt aus Korrekturen)
- **Aufgaben** automatisch aus E-Mails extrahieren
- **Täglicher Audio-Digest** – Zusammenfassung der letzten E-Mails zum Anhören (Web-Player & privater Podcast-Feed)
- **Frag deine Inbox** – RAG-Suche mit Quellenangaben
- **Enterprise-ready** – Erst-Login wird Admin; Anmeldung über Microsoft Entra ID, Google, GitHub, OIDC oder LDAP/Active Directory
- **Datenschutz** – läuft vollständig lokal mit Ollama, auch nur auf CPU; DSGVO by design

## Analyse-Werkzeug, kein Mail-Client

ollamail liest Postfächer und wertet sie aus; Mails verwalten Sie weiter in Ihrem Mail-Client.
Auf den Mailserver zurück gehen nur diese Aktionen:

- **Gelesen/ungelesen** (eigene Postfächer)
- **Antworten senden** – nur auf ausdrücklichen Klick, nur aus eigenen Postfächern
- **Triage-Kategorie als Label oder Ordner** – nur, wenn pro Postfach eingeschaltet

Archivieren, Verschieben, Löschen und Markieren (Flag/Stern) sind derzeit **nicht** möglich, ebenso
neue Mails verfassen. Details: [Architektur §3.1](docs/ARCHITECTURE.md#mail-aktionen-auf-dem-server).

> **Status:** Vorbereitung auf die erste Version `v0.1.0` – Funktionsumfang und bekannte
> Einschränkungen im [Changelog](CHANGELOG.md). Vor 1.0 können sich Konfiguration und API noch
> ändern. Siehe auch die [Roadmap](docs/ROADMAP.md).

## Schnellstart in 5 Minuten

Voraussetzungen: Linux-Host mit Docker Engine und Compose v2.24+, `git`, `openssl`. Der erste
Build der Images dauert je nach Rechner länger als fünf Minuten.

```sh
git clone https://github.com/dusseligerdussel/ollamail.git && cd ollamail

# Konfiguration mit frischen Secrets
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
sed -i "s|^OLLAMAIL_SECRET_KEY=.*|OLLAMAIL_SECRET_KEY=$(openssl rand -base64 32)|" deploy/.env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" deploy/.env

# Stack bauen und starten, mit lokalem Ollama (CPU) und den Modellen des Profils `cpu`
docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml --profile ollama-cpu up -d --build --wait
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull qwen2.5:3b
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull bge-m3

# Setup-Code für den ersten Admin
docker compose -f deploy/compose.yaml exec api python -m app.cli setup-token
```

Dann <http://localhost:8080> öffnen, mit dem Setup-Code den ersten Admin anlegen und unter
Einstellungen → Postfächer ein Postfach verbinden. Die UI ist standardmäßig nur auf dem Host
selbst erreichbar (`OLLAMAIL_HTTP_BIND=127.0.0.1`). Für den Zugriff über das Netz TLS davorsetzen
(Sitzungs-Cookies sind `Secure`): Über `http://<LAN-IP>:8080` speichern Browser die Cookies nicht,
Setup und Anmeldung schlagen dann mit dem Hinweis „Unverschlüsselte Verbindung“ fehl. Nur für Tests
im eigenen Netz helfen `OLLAMAIL_HTTP_BIND` und `OLLAMAIL_AUTH_COOKIE_SECURE=false` – mit
unverschlüsselten Passwörtern und Sitzungen, siehe
[Betrieb 2.6](docs/OPERATIONS.md#26-http-ohne-tls-testbetrieb). Alles Weitere – fertige Images, Reverse Proxy, Backup,
Updates, Fehlersuche – steht in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Dokumentation

- [Betrieb: Installation, Backup, Updates](docs/OPERATIONS.md)
- [Architektur](docs/ARCHITECTURE.md)
- [Datenschutz & Sicherheit](docs/PRIVACY.md)
- [Design-Richtlinien](docs/DESIGN.md)
- [Roadmap](docs/ROADMAP.md)
- [Mitarbeit / Regeln für Agenten](CLAUDE.md)

## Lizenz

[AGPL-3.0](LICENSE)
