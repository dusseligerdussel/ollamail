# Betrieb

Handbuch für Admins, die ollamail selbst betreiben: Installation, Hardware, Reverse Proxy,
Backup/Restore, Updates, Datenschutz und Fehlersuche.

> **Projektstatus:** ollamail ist im frühen Aufbau. Lauffähig sind heute der Compose-Stack,
> die Datenbank mit Migrationen, die API mit Health-Endpunkten, lokale Anmeldung (API) und die
> UI-Shell. Mail-Sync, KI-Funktionen, externe Identity-Provider und Worker gibt es noch nicht. Was noch nicht existiert, ist in diesem
> Dokument mit **geplant (#nr)** markiert und verweist auf das zugehörige Issue.

Referenz für Dienste, Profile, Volumes und Entwicklungsmodus: [`deploy/README.md`](../deploy/README.md).
Alle Einstellungen: [`deploy/.env.example`](../deploy/.env.example).

## Inhalt

1. [Systemanforderungen](#1-systemanforderungen)
2. [Installation](#2-installation)
3. [Hardware-Profile und LLM](#3-hardware-profile-und-llm)
4. [Reverse Proxy und TLS](#4-reverse-proxy-und-tls)
5. [Backup und Restore](#5-backup-und-restore)
6. [Updates und Migrationen](#6-updates-und-migrationen)
7. [Schlüsselverwaltung](#7-schlüsselverwaltung)
8. [Skalierung](#8-skalierung)
9. [Datenschutz-Hinweise für Betreiber](#9-datenschutz-hinweise-für-betreiber)
10. [Troubleshooting](#10-troubleshooting)

## 1. Systemanforderungen

| Komponente | Anforderung |
|---|---|
| Betriebssystem | Linux-Host mit Docker Engine (amd64 oder arm64) |
| Docker | Docker Engine mit **Compose v2.24 oder neuer** (`env_file` mit `required` wird verwendet) |
| Netzwerk | Beim Bauen Zugriff auf Docker Hub, Debian-Paketquellen, PyPI und npm. Zur Laufzeit keiner nötig, außer zu Mailservern, – falls genutzt – zu einem externen LLM-Server und einmalig zu `huggingface.co` für die TTS-Stimmen (abschaltbar, siehe [3.6](#36-sprachausgabe-tts)) |
| Ports | Ein freier Port für die UI, Standard `8080` (`OLLAMAIL_HTTP_PORT`) |
| Werkzeuge | `git`, `openssl` (zum Erzeugen von Secrets) |

Der Basis-Stack (UI, API, PostgreSQL) ist schlank. Den Großteil von CPU, RAM und Speicher
benötigt später das LLM, siehe [Abschnitt 3](#3-hardware-profile-und-llm).

Vorgebaute Images gibt es noch nicht (Multi-Arch-Images über GHCR: **geplant (#10)**). Die Images
werden beim ersten Start auf dem Host gebaut, für dessen Architektur.

## 2. Installation

### 2.1 Code holen

```sh
git clone https://github.com/dusseligerdussel/ollamail.git
cd ollamail
```

Alle folgenden Befehle laufen im Wurzelverzeichnis des Repositorys.

### 2.2 Konfiguration anlegen

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

In `deploy/.env` mindestens setzen:

| Variable | Wert |
|---|---|
| `OLLAMAIL_SECRET_KEY` | Zufälliger Master-Key, z. B. Ausgabe von `openssl rand -base64 32`. Wird ab #6 zum Verschlüsseln gespeicherter Zugangsdaten verwendet. **Sicher aufbewahren**, siehe [Abschnitt 7](#7-schlüsselverwaltung). |
| `POSTGRES_PASSWORD` | Datenbank-Passwort, z. B. `openssl rand -hex 24`. Hex vermeidet Sonderzeichen, die in `OLLAMAIL_DATABASE_URL` URL-kodiert werden müssten. |

Wichtig:

- `POSTGRES_PASSWORD` wird nur beim **allerersten Start** (leeres Datenbank-Volume) übernommen.
  Später geänderte Werte in `.env` ändern das Passwort in der Datenbank nicht.
- `OLLAMAIL_DATABASE_URL` setzt sich per Default aus `POSTGRES_USER`, `POSTGRES_PASSWORD` und
  `POSTGRES_DB` zusammen und muss nur bei einer externen Datenbank angepasst werden.
- `deploy/.env` ist per `.gitignore` vom Commit ausgeschlossen. Die Datei enthält alle Secrets der
  Instanz – sichern, aber nicht weitergeben.

### 2.3 Starten

```sh
docker compose -f deploy/compose.yaml up -d --build
```

Beim Start passiert der Reihe nach:

1. `postgres` startet und wird `healthy`.
2. `migrate` führt `alembic upgrade head` aus und beendet sich (Status `Exited (0)` ist korrekt).
3. `api` startet, danach `frontend`.

Der erste Build dauert einige Minuten.

### 2.4 Prüfen

```sh
docker compose -f deploy/compose.yaml ps
curl http://localhost:8080/api/healthz   # {"status":"ok"}
curl http://localhost:8080/api/readyz    # {"status":"ok","checks":{"database":"ok"}}
```

| Endpunkt | Bedeutung |
|---|---|
| `/api/healthz` | Liveness: Der API-Prozess läuft. |
| `/api/readyz` | Readiness: `200`, wenn alle Abhängigkeiten erreichbar sind, sonst `503` mit der fehlgeschlagenen Prüfung. Heute wird nur `database` geprüft; Prüfungen für Queue und LLM kommen mit #7 und #17. |

Die UI ist unter `http://<host>:8080` erreichbar. Der Setup-Assistent der UI ist **geplant (#12)**,
externe Identity-Provider (OIDC, GitHub, LDAP) **geplant (#30–#33)**.

**Erst-Admin:** Solange kein Nutzer existiert, legt `POST /api/setup` den ersten Admin an. Dafür
ist ein Setup-Token nötig – `OLLAMAIL_SETUP_TOKEN` oder, falls leer, ein aus `OLLAMAIL_SECRET_KEY`
abgeleiteter Wert. Die API schreibt ihn beim Start ins Log (Event `setup_pending`, Feld
`setup_code`), solange die Instanz nicht eingerichtet ist:

```sh
docker compose -f deploy/compose.yaml logs api | grep setup_pending
docker compose -f deploy/compose.yaml run --rm --no-deps api python -m app.cli setup-token
```

Nach dem Setup ist der Token wertlos. Notfallzugang ohne UI (z. B. ausgesperrt):
`docker compose -f deploy/compose.yaml run --rm api python -m app.cli create-admin`.

**Cookies nur über HTTPS:** Sitzungs-Cookies sind `Secure`. Browser speichern sie über
`http://<ip>:8080` nicht (Ausnahme `http://localhost`); die Anmeldung schlägt dann fehl. Also TLS
davorsetzen oder – nur für Testinstallationen – `OLLAMAIL_AUTH_COOKIE_SECURE=false`.

Für den Betrieb im Netz unbedingt TLS davorsetzen: [Abschnitt 4](#4-reverse-proxy-und-tls).

### 2.5 Stoppen

```sh
docker compose -f deploy/compose.yaml down      # Container stoppen, Daten bleiben erhalten
```

`down -v` löscht zusätzlich **alle Volumes inklusive Datenbank**. Nur verwenden, wenn die Daten
wirklich weg sollen.

## 3. Hardware-Profile und LLM

> Die LLM-Anbindung ist **geplant (#17)**, Modellwahl je Aufgabe im Admin-UI **geplant (#18)**.
> Die Anwendung ruft heute noch kein LLM auf; `OLLAMAIL_LLM_BASE_URL` steht bereits in
> `.env.example`, wird aber erst mit #17 ausgewertet. Die Compose-Profile für Ollama existieren
> schon und können vorbereitend genutzt werden.

ollamail läuft auch ohne GPU. Eine GPU beschleunigt nur, sie ist keine Voraussetzung.

### 3.1 Profile im Überblick

Richtwerte aus [`ARCHITECTURE.md`](ARCHITECTURE.md#32-llm-provider). Konkrete Modellempfehlungen
und gemessene Durchsätze folgen mit den Benchmarks aus Triage (#20); TTS siehe
[3.6](#36-sprachausgabe-tts).

| Profil | Zielhardware | Chat/Klassifikation | Betrieb |
|---|---|---|---|
| CPU-only | 16 GB RAM, keine GPU | ~1–4B-Modell, quantisiert | Compose-Profil `ollama-cpu` |
| Consumer-GPU | NVIDIA, 8–24 GB VRAM | ~7–14B-Modell | Compose-Profil `ollama-gpu` |
| Server-GPU | Rechenzentrums-GPUs | ≥ 30B-Modell, hoher Durchsatz | Externer vLLM-Server (OpenAI-kompatibel) |

Für Embeddings ist ein mehrsprachiges Modell wie `bge-m3` vorgesehen.

Empfehlung für CPU-only (Planungsgrundlage, gilt ab #17/#28): Triage und Aufgaben-Extraktion mit
einem kleinen Modell laufen lassen, den Daily Digest in die Nacht legen.

### 3.2 CPU-only (Profil `ollama-cpu`)

```sh
docker compose -f deploy/compose.yaml --profile ollama-cpu up -d
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull <modell>
```

Ollama ist nur im internen Compose-Netz unter `http://ollama:11434` erreichbar, der Port wird nicht
veröffentlicht. Modelle liegen im Volume `ollama-models`.

Das Profil muss bei **jedem** `docker compose`-Aufruf mit angegeben werden, der den Dienst
betreffen soll (`up`, `down`, `exec`, `logs`). Alternativ `COMPOSE_PROFILES=ollama-cpu` in der
Shell setzen.

### 3.3 Consumer-GPU (Profil `ollama-gpu`)

Voraussetzungen auf dem Host:

1. Aktueller NVIDIA-Treiber (`nvidia-smi` funktioniert auf dem Host).
2. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   installiert und für Docker konfiguriert.

```sh
docker compose -f deploy/compose.yaml --profile ollama-gpu up -d
docker compose -f deploy/compose.yaml --profile ollama-gpu logs ollama-gpu   # erkannte GPU prüfen
docker compose -f deploy/compose.yaml exec ollama-gpu ollama pull <modell>
```

Der Container reserviert alle GPUs des Hosts. Nur eines der beiden Ollama-Profile gleichzeitig
starten – beide melden sich im Netz als `ollama`.

### 3.4 Server-GPU mit vLLM

**Geplant (#17):** Anbindung OpenAI-kompatibler Endpunkte. vLLM läuft dann als eigener Dienst
(eigener Host oder eigene Compose-Datei, nicht Teil von `deploy/compose.yaml`) und wird über seine
OpenAI-kompatible API angebunden. Die genaue Konfiguration wird mit #17/#18 hier ergänzt.

### 3.5 Externer Ollama-Server

Ohne Ollama-Profil kann ein vorhandener Ollama-Server genutzt werden (wirksam ab #17):

- Anderer Host: `OLLAMAIL_LLM_BASE_URL=http://<host>:11434`
- Ollama direkt auf dem Docker-Host: `OLLAMAIL_LLM_BASE_URL=http://host.docker.internal:11434`.
  Unter Linux braucht der Container dafür einen `extra_hosts`-Eintrag
  (`host.docker.internal:host-gateway`) in einer eigenen Compose-Override-Datei.

Cloud-LLMs sind standardmäßig gesperrt (`OLLAMAIL_LLM_CLOUD_ENABLED=false`), siehe
[Abschnitt 9](#9-datenschutz-hinweise-für-betreiber).

### 3.6 Sprachausgabe (TTS)

Standard ist **Piper** (ADR 4), Teil des Backend-Images; ffmpeg kodiert nach Opus und MP3. Die
Stimmen sind nicht im Image: Der Worker lädt fehlende Standardstimmen beim ersten Bedarf und
täglich per Job `tts.ensure_voices` (Queue `tts`) ins Daten-Volume nach
`/data/tts/voices/piper/` (je Stimme `<id>.onnx` und `<id>.onnx.json`, 60–120 MB).

| Sprache | Standardstimme | Datensatz / Lizenz |
|---|---|---|
| Deutsch | `de_DE-thorsten-medium` (`OLLAMAIL_TTS_VOICE_DE`) | Thorsten-Voice, CC0-1.0 |
| Englisch | `en_US-ljspeech-medium` (`OLLAMAIL_TTS_VOICE_EN`) | LJ Speech, Public Domain |

Andere Stimmen aus [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) lassen
sich über die Variablen einstellen. Vorher die Lizenz im `MODEL_CARD` der Stimme prüfen: Einige
Trainingsdatensätze haben Nutzungsbeschränkungen (z. B. nur nicht-kommerziell).

**Ohne Internetzugang:** `OLLAMAIL_TTS_DOWNLOAD_VOICES=false` setzen und beide Dateien je Stimme
manuell ins Volume kopieren:

```sh
docker compose -f deploy/compose.yaml cp de_DE-thorsten-medium.onnx worker:/data/tts/voices/piper/
docker compose -f deploy/compose.yaml cp de_DE-thorsten-medium.onnx.json worker:/data/tts/voices/piper/
```

**Durchsatz** (gemessen in #27, Intel Xeon 2,1 GHz, 4 Kerne, CPU-only, englische Stimme der
Qualität `medium`; die Standardstimmen haben dieselbe Modellgröße):
2.000 Zeichen Text ergeben rund 160 s Audio und brauchen inklusive Normalisierung und
Kodierung nach Opus und MP3 etwa 9–10 s, also rund 6 % der Echtzeit. Die Synthese läuft pro
Worker-Prozess nacheinander und nutzt dabei alle Kerne.

### 3.7 Suchindex und Embedding-Modell

Jede neue Mail durchläuft den Verarbeitungsschritt `index` (Queue `llm`): Mailtext (ohne Zitate
und Signatur) und Text aus Anhängen (PDF, DOCX, TXT, HTML; kein OCR) werden in Abschnitte
(„Chunks“) zerlegt, in PostgreSQL volltextindiziert und mit dem Embedding-Modell
(Standard `bge-m3`, Aufgabe `embeddings`) in Vektoren umgerechnet. Die Suche kombiniert beide
Indizes. Ist das LLM beim Indizieren nicht erreichbar, ist die Mail trotzdem sofort per Volltext
auffindbar; der Job `search.fill_embeddings` (alle 5 Minuten, hinter neuen Mails) ergänzt die
Vektoren später.

Anhänge werden in einem eigenen Prozess ohne Umgebungsvariablen und mit Grenzen für Größe,
Laufzeit und Speicher gelesen (`OLLAMAIL_SEARCH_ATTACHMENT_MAX_BYTES`,
`OLLAMAIL_SEARCH_EXTRACTION_TIMEOUT`, `OLLAMAIL_SEARCH_EXTRACTION_MAX_MEMORY_MB`).

**Stand prüfen:**

```sh
docker compose -f deploy/compose.yaml run --rm api python -m app.cli search status
```

zeigt Anzahl der Chunks, Vektoren je Modell, das aktive und das konfigurierte Modell sowie die
Vektor-Dimension.

**Modellwechsel mit gleicher Dimension** (z. B. ein anderes Modell mit 1024 Dimensionen):
`OLLAMAIL_LLM_TASK_EMBEDDINGS_MODEL` (oder `OLLAMAIL_LLM_DEFAULT_EMBEDDING_MODEL`) setzen und
Worker neu starten. Der Job `search.fill_embeddings` berechnet die Vektoren aller Chunks im
Hintergrund neu (`OLLAMAIL_SEARCH_REEMBED_BATCH_SIZE` je Durchlauf, neueste Mails zuerst).
Bis er fertig ist, beantworten die Vektoren des **alten** Modells die Suchanfragen; das alte
Modell muss dafür auf dem Endpunkt installiert bleiben. Danach schaltet der Job um und löscht die
alten Vektoren. Während des Wechsels werden neue Mails mit beiden Modellen eingebettet.

**Modellwechsel mit anderer Dimension** (z. B. von 1024 auf 768): Die Vektorspalte hat eine feste
Länge, alte und neue Vektoren können nicht nebeneinander liegen.

1. Neues Modell und `OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS=<n>` in `deploy/.env` setzen (höchstens
   2000, Grenze des HNSW-Index).
2. Worker stoppen: `docker compose -f deploy/compose.yaml stop worker`.
3. Spalte umstellen: `docker compose -f deploy/compose.yaml run --rm api python -m app.cli search resize`.
   Das löscht alle Vektoren, ändert die Spalte auf `vector(<n>)` und legt den HNSW-Index neu an
   (in einer Transaktion).
4. Worker starten. `search.fill_embeddings` baut die Vektoren im Hintergrund neu auf. Bis dahin
   findet die Suche Mails nur per Volltext bzw. mit den schon neu berechneten Vektoren.

Eine Neu-Indizierung inklusive Chunking (z. B. nach geänderter Chunk-Größe) startet
`python -m app.cli processing reprocess --step index`.

## 4. Reverse Proxy und TLS

Der `frontend`-Container spricht nur HTTP auf Port 8080 und ist der einzige veröffentlichte Port.
TLS terminiert ein vorgeschalteter Reverse Proxy. Dafür in `deploy/.env`:

```sh
OLLAMAIL_HTTP_BIND=127.0.0.1   # UI nur noch lokal erreichbar, nicht direkt aus dem Netz
```

und den Stack neu starten (`docker compose -f deploy/compose.yaml up -d`).

Anforderungen an den Proxy:

- **Gesamten Pfad** (`/`) an `http://127.0.0.1:8080` weiterleiten. UI und API (`/api`) laufen über
  denselben Origin; die Content-Security-Policy erlaubt keine fremden Origins.
- `Host`, `X-Forwarded-For` und `X-Forwarded-Proto` setzen. Der interne Caddy übernimmt
  `X-Forwarded-*` nur von privaten Netzen (RFC 1918, Loopback); ein Proxy auf demselben Host oder
  im selben LAN erfüllt das.
- **Server-Sent Events** (Live-Updates, gestreamte Antworten; geplant mit #7 und #25):
  Antwort-Pufferung abschalten und lange Verbindungen erlauben. Der interne Caddy nutzt dafür
  bereits `flush_interval -1` und 1 h Timeout – der äußere Proxy muss mindestens genauso großzügig
  sein.

Die folgenden Beispiele verwenden `mail.example.org` als Hostnamen.

### 4.1 Caddy

Caddy holt sich das Zertifikat automatisch über Let's Encrypt (Ports 80/443 müssen erreichbar sein).

```caddyfile
mail.example.org {
	reverse_proxy 127.0.0.1:8080 {
		# Server-Sent Events sofort durchreichen
		flush_interval -1
	}
}
```

Caddy setzt `X-Forwarded-*` automatisch.

### 4.2 nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name mail.example.org;

    ssl_certificate     /etc/ssl/mail.example.org/fullchain.pem;
    ssl_certificate_key /etc/ssl/mail.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        # Server-Sent Events: nicht puffern, lange Verbindungen zulassen
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}

server {
    listen 80;
    server_name mail.example.org;
    return 301 https://$host$request_uri;
}
```

`http2 on;` benötigt nginx 1.25.1 oder neuer; bei älteren Versionen stattdessen
`listen 443 ssl http2;`. Zertifikate z. B. per certbot.

### 4.3 Traefik

Beispiel mit File-Provider (dynamische Konfiguration), Traefik v3. Der Entrypoint `websecure` und
ein Certificate Resolver (hier `letsencrypt`) sind in der statischen Traefik-Konfiguration definiert.

```yaml
http:
  routers:
    ollamail:
      rule: Host(`mail.example.org`)
      entryPoints: [websecure]
      service: ollamail
      tls:
        certResolver: letsencrypt
  services:
    ollamail:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8080
        responseForwarding:
          # Server-Sent Events sofort durchreichen
          flushInterval: -1
```

Läuft Traefik selbst als Container, ist `127.0.0.1` dort der Traefik-Container. Dann die
IP-Adresse des Docker-Hosts eintragen und `OLLAMAIL_HTTP_BIND` auf diese Adresse setzen – oder
Traefik und ollamail in ein gemeinsames Docker-Netz hängen.

### 4.4 Sicherheits-Header

Der interne Caddy setzt bereits CSP, `X-Frame-Options`, `Referrer-Policy` u. a.
(Details: [`deploy/README.md`](../deploy/README.md#sicherheits-header)). Der äußere Proxy sollte diese
nicht überschreiben. HSTS (`Strict-Transport-Security`) setzt ollamail nicht, da es TLS nicht
selbst terminiert – bei Bedarf im äußeren Proxy ergänzen.

## 5. Backup und Restore

Ein vollständiges Backup besteht aus **drei Teilen**:

| Teil | Inhalt | Ohne ihn … |
|---|---|---|
| `deploy/.env` | `OLLAMAIL_SECRET_KEY`, Datenbank-Passwort, alle Einstellungen | … sind verschlüsselt gespeicherte Zugangsdaten (ab #6) **unwiederbringlich verloren** |
| Datenbank | Alle Anwendungsdaten (Volume `postgres-data`) | … ist die Instanz leer |
| Daten-Volume | Anhänge, Audio-Digests (Volume `ollamail-data`, in den Containern `/data`) | … fehlen Dateien |

Das Volume `ollama-models` muss nicht gesichert werden; Modelle lassen sich neu laden.

> **Den Secret Key getrennt vom Datenbank-Backup aufbewahren** (z. B. im Passwort-Manager oder
> Tresor). Wer Backup und Key zusammen hat, kann die gespeicherten Zugangsdaten entschlüsseln.

Volumes heißen auf dem Host `<projekt>_<volume>`, mit dem Standard-Projektnamen also
`ollamail_postgres-data` und `ollamail_ollamail-data` (`docker volume ls`).

### 5.1 Backup erstellen

Im Wurzelverzeichnis des Repositorys, bei laufendem Stack. Das Zielverzeichnis liegt bewusst
**außerhalb** des Repositorys, damit Backups nicht versehentlich committet werden:

```sh
BACKUP_DIR=/var/backups/ollamail   # beliebig, außerhalb des Repositorys
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"

# 1. Konfiguration inkl. Secret Key
cp deploy/.env "$BACKUP_DIR/ollamail.env"

# 2. Datenbank (konsistenter Snapshot, auch im laufenden Betrieb)
docker compose -f deploy/compose.yaml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$BACKUP_DIR/ollamail-db.dump"

# 3. Daten-Volume
docker run --rm -v ollamail_ollamail-data:/data:ro -v "$BACKUP_DIR":/backup alpine:3 \
  tar czf /backup/ollamail-data.tar.gz -C /data .
```

Hinweise:

- Das Backup-Verzeichnis enthält Secrets und Mail-Daten: verschlüsselt und zusätzlich außerhalb
  des Hosts aufbewahren.
- Das Daten-Volume wird im laufenden Betrieb gesichert. Für ein exakt zur Datenbank passendes
  Backup vorher `docker compose -f deploy/compose.yaml stop api worker` und danach wieder
  `start api worker` ausführen.
- Regelmäßig per cron/systemd-Timer ausführen und mindestens einmal einen Restore-Test machen.

### 5.2 Restore auf einer leeren Instanz

Gilt für einen neuen Host oder nach `down -v`. Voraussetzung: Repository auf derselben oder einer
neueren Version wie beim Backup (siehe [Abschnitt 6](#6-updates-und-migrationen)).

```sh
BACKUP_DIR=/var/backups/ollamail

# 1. Konfiguration zurückspielen (Secret Key und Passwort müssen zum Backup passen)
cp "$BACKUP_DIR/ollamail.env" deploy/.env
chmod 600 deploy/.env

# 2. Container und Volumes anlegen, nur die Datenbank starten (leeres Volume)
docker compose -f deploy/compose.yaml create
docker compose -f deploy/compose.yaml up -d --wait postgres

# 3. Datenbank einspielen
docker compose -f deploy/compose.yaml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$BACKUP_DIR/ollamail-db.dump"

# 4. Daten-Volume einspielen
docker run --rm -v ollamail_ollamail-data:/data -v "$BACKUP_DIR":/backup:ro alpine:3 \
  sh -c 'tar xzf /backup/ollamail-data.tar.gz -C /data && chown -R 10001:10001 /data'

# 5. Rest starten (migrate bringt das Schema ggf. auf den aktuellen Stand)
docker compose -f deploy/compose.yaml up -d --wait
curl http://localhost:8080/api/readyz
```

`--exit-on-error` bricht beim ersten Fehler ab. Ein Fehler in Schritt 3 bedeutet meist, dass die
Datenbank nicht leer war – Restore immer in ein **leeres** Volume.

Das Daten-Volume gehört dem App-Nutzer mit UID 10001 (`chown` in Schritt 4).

### 5.3 Restore-Test ohne Produktionsdaten zu berühren

Ein zweiter Compose-Projektname (`-p`) erzeugt eigene Container und Volumes neben der laufenden
Instanz. Auf demselben Host einen anderen UI-Port wählen:

```sh
BACKUP_DIR=/var/backups/ollamail
T="docker compose -p ollamail-restoretest -f deploy/compose.yaml"

$T create
$T up -d --wait postgres
$T exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$BACKUP_DIR/ollamail-db.dump"
OLLAMAIL_HTTP_PORT=8081 $T up -d --wait
curl http://localhost:8081/api/readyz

# Aufräumen: löscht nur die Test-Instanz und ihre Volumes
$T down -v
```

Für einen vollständigen Test auch das Daten-Volume einspielen (wie in 5.2, Volume-Name
`ollamail-restoretest_ollamail-data`).

## 6. Updates und Migrationen

### 6.1 Update aus dem Repository

```sh
# 1. Backup (Abschnitt 5.1)

# 2. Neue Version holen
git pull

# 3. Neu bauen und starten
docker compose -f deploy/compose.yaml up -d --build
```

Datenbank-Migrationen laufen **automatisch**: Der Dienst `migrate` führt vor jedem Start von
`api`/`worker` `alembic upgrade head` aus. Schlägt eine Migration fehl, starten `api` und
`frontend` nicht; die Ursache steht in `docker compose -f deploy/compose.yaml logs migrate`.

Aktuellen Schema-Stand anzeigen:

```sh
docker compose -f deploy/compose.yaml run --rm migrate alembic current
```

Neue Variablen in `deploy/.env.example` nach jedem Update mit der eigenen `deploy/.env`
vergleichen, z. B. mit `diff deploy/.env.example deploy/.env`.

**Zurück auf eine ältere Version:** Einen automatischen Downgrade-Weg gibt es nicht. Ältere Version
auschecken und das Backup von **vor** dem Update einspielen (Abschnitt 5.2).

### 6.2 Vorgebaute Images

**Geplant (#10):** Multi-Arch-Images (amd64/arm64) in der GitHub Container Registry. Die
Variablen `OLLAMAIL_API_IMAGE` und `OLLAMAIL_FRONTEND_IMAGE` existieren bereits in
`deploy/.env.example`; Tags und Update-Ablauf (`pull` statt `--build`) werden mit #10 hier ergänzt.

### 6.3 Drittanbieter-Images

PostgreSQL (`POSTGRES_IMAGE`, Standard `pgvector/pgvector:pg16`) und Ollama (`OLLAMA_IMAGE`) sind
über Variablen in `deploy/.env` festgelegt. Ein Wechsel der **PostgreSQL-Hauptversion** (z. B. 16 → 17)
ist mit dem bestehenden Volume nicht möglich: Backup ziehen, neue Version mit leerem Volume
starten, Backup einspielen (Abschnitt 5).

## 7. Schlüsselverwaltung

`OLLAMAIL_SECRET_KEY` ist der Master-Key für die Verschlüsselung gespeicherter Zugangsdaten
(Postfach-Passwörter, OAuth-Tokens, IdP-Secrets; Verfahren siehe [`PRIVACY.md`](PRIVACY.md)).

- Das Krypto-Modul ist **geplant (#6)**; heute speichert ollamail noch keine Secrets. Den Key
  trotzdem **jetzt** erzeugen und sichern – sobald #6 aktiv ist, gilt:
- Geht der Key verloren, sind gespeicherte Zugangsdaten nicht mehr lesbar. Postfächer und
  Identity-Provider müssen dann neu eingerichtet werden.
- Den Key nach der Einrichtung nicht einfach in `.env` austauschen.

**Key-Rotation** ist **geplant (#6)**. Ablauf und Befehle werden mit #6 hier dokumentiert.

## 8. Skalierung

Heute läuft genau eine API-Instanz; Hintergrundjobs gibt es noch nicht.

**Geplant (#7):** Der `worker` (Procrastinate, Queue in PostgreSQL) übernimmt Mail-Sync,
KI-Verarbeitung und TTS. Vorgesehen sind mehrere Worker-Instanzen und nach Jobtyp getrennte Queues
(`sync`, `llm`, `tts`), sodass z. B. ein Worker nur LLM-Jobs auf einem GPU-Host abarbeitet. Bis
dahin startet `worker` nur mit `--profile worker` und bricht mangels Code ab. Konkrete Befehle
folgen mit #7.

**Mail-Sync (IMAP):** Jeder Worker, der die Queue `sync` abarbeitet, hält zusätzlich eine
Datenbankverbindung für die Verteilung der Postfächer (Advisory-Locks) und pro überwachtem
Postfach eine dauerhafte IMAP-Verbindung (`IDLE`); während eines Syncs kommt eine zweite hinzu.
Mailserver begrenzen gleichzeitige Verbindungen pro Nutzer (Dovecot: `mail_max_userip_connections`,
Standard 10) – zwei pro Postfach reichen. Laufen mehrere Worker mit `sync`, übernimmt jeder einen
Teil der Postfächer; fällt einer aus, übernehmen die anderen innerhalb einer Minute.
Mailserver mit selbstsigniertem Zertifikat: das CA-Zertifikat dem Container über
`SSL_CERT_FILE` bekannt machen; `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true` (keine Prüfung,
auch unverschlüsselt) nur in Testumgebungen.

Was heute schon gilt: Jeder API- bzw. Worker-Prozess öffnet bis zu
`OLLAMAIL_DATABASE_POOL_SIZE + OLLAMAIL_DATABASE_MAX_OVERFLOW` Datenbankverbindungen (Standard
5 + 10). Beim Hochskalieren darauf achten, dass die Summe unter `max_connections` von PostgreSQL
bleibt (PostgreSQL-Standard: 100).

## 9. Datenschutz-Hinweise für Betreiber

Grundlage für Verarbeitungsverzeichnis und DSFA. Grundsätze und technische Maßnahmen:
[`PRIVACY.md`](PRIVACY.md). Die Spalte „Status“ zeigt, was die aktuelle Version tatsächlich
verarbeitet.

### 9.1 Datenkategorien und Speicherorte

| Datenkategorie | Speicherort | Status |
|---|---|---|
| Nutzerkonten, Rollen | PostgreSQL (`postgres-data`): `users`, `auth_identities` (Passwörter als Argon2id-Hash) | aktiv |
| Gruppen | PostgreSQL | geplant (#30–#33) |
| Sessions | PostgreSQL: `auth_sessions` (nur SHA-256 des Cookie-Tokens, Browser-Kennung gekürzt); abgelaufene stündlich gelöscht | aktiv |
| Login-Zähler (Rate-Limit, Sperre) | PostgreSQL: `auth_rate_limits` (nur HMAC von IP bzw. E-Mail-Adresse); stündlich bereinigt | aktiv |
| Postfach-Zugangsdaten, OAuth-Tokens, IdP-Secrets | PostgreSQL, verschlüsselt mit `OLLAMAIL_SECRET_KEY` | geplant (#6, #15) |
| E-Mails (Header, Inhalte, Metadaten) | PostgreSQL | geplant (#13, #14) |
| Anhänge | Daten-Volume (`ollamail-data`) | geplant (#13) |
| KI-Ergebnisse: Triage, Aufgaben | PostgreSQL | geplant (#20, #22) |
| Suchindex: Text-Abschnitte von Mails und Anhängen, Volltextindex, Embeddings | PostgreSQL: `search_chunks`, `search_embeddings` (pgvector); hängen per `ON DELETE CASCADE` an Mail, Anhang und Postfach | vorhanden (#24) |
| Chat-Verläufe („Frag deine Inbox“) | PostgreSQL | geplant (#25) |
| Daily Digest: Text und Audio | PostgreSQL bzw. Daten-Volume | geplant (#28) |
| Audit-Log (Ereignistyp, Zeitpunkt, Nutzer- bzw. Objekt-ID, Codes und Zähler; keine Inhalte, Betreffzeilen oder Adressen) | PostgreSQL: `audit_events`, append-only; Aufbewahrung `OLLAMAIL_AUDIT_RETENTION_DAYS` (Durchsetzung #36) | aktiv |
| Job-Queue | PostgreSQL | geplant (#7) |
| Verarbeitungsstatus je Mail und Schritt (Version, Status, Fehlercode; keine Inhalte) | PostgreSQL (`message_processing`) | vorhanden (#19) |
| LLM-Modelle (keine personenbezogenen Daten) | Volume `ollama-models` | vorhanden (Profil `ollama-*`) |
| TTS-Stimmen (keine personenbezogenen Daten) | Daten-Volume, `tts/voices/<engine>/` | vorhanden (#27) |
| Instanz-Secrets und Konfiguration | `deploy/.env` auf dem Host | vorhanden |
| Betriebslogs (ohne Mail-Inhalte, siehe 9.3) | Docker-Logging des Hosts | vorhanden |

Aufbewahrungsfristen, Export und Löschung: **geplant (#36)**.

### 9.2 Datenflüsse

```
Browser ──HTTPS──▶ Reverse Proxy ──HTTP──▶ frontend (Caddy) ──▶ api ──▶ PostgreSQL
                                                                   ▲
                       worker (geplant #7) ────────────────────────┘
                         │
                         ├──▶ Mailserver: IMAP (#14) / Microsoft Graph / Gmail API (geplant #37, #38)
                         ├──▶ LLM: Ollama im Compose-Netz oder eigener Server (geplant #17)
                         ├──▶ huggingface.co: Download fehlender TTS-Stimmen, sendet keine Daten (#27)
                         └──▶ Cloud-LLM nur bei OLLAMAIL_LLM_CLOUD_ENABLED=true (geplant #17, #18)

api ──▶ Identity-Provider: OIDC / LDAP (geplant #30–#32)
```

- **Standardmäßig verlassen keine Daten die Instanz.** Externe Verbindungen entstehen nur zu den
  Mailservern und Identity-Providern, die der Admin konfiguriert, und zu einem LLM-Server, den er
  einträgt.
- **Cloud-LLMs** sind ein globaler Admin-Schalter, Standard `OLLAMAIL_LLM_CLOUD_ENABLED=false`.
  Wird er aktiviert, gehen Mail-Inhalte an den jeweiligen Anbieter. Das ist dann eine
  Auftragsverarbeitung bzw. Drittlandübermittlung, die der Betreiber vertraglich absichern muss.
- **Keine Telemetrie**, keine externen Fonts oder CDNs. Die Content-Security-Policy der UI erlaubt
  nur den eigenen Origin.

### 9.3 Logs

- Die API loggt strukturiert als JSON auf stdout. Ein PII-Filter verwirft u. a. Betreffzeilen,
  Adressen, Inhalte, Prompts und Tokens; Zugriffslogs enthalten nur Routen-Templates
  (Details: [`PRIVACY.md`](PRIVACY.md#logging-im-detail)).
- Logs landen im Docker-Logging des Hosts (`docker compose -f deploy/compose.yaml logs`). Der
  Docker-Standardtreiber `json-file` **rotiert nicht**: Rotation in `/etc/docker/daemon.json`
  einrichten (`"log-opts": {"max-size": "10m", "max-file": "5"}`) und die Aufbewahrung in das
  Löschkonzept aufnehmen.
- Der Log-Level `DEBUG` (`OLLAMAIL_LOG_LEVEL`) ist nur zur Fehlersuche gedacht.

### 9.4 Hinweise für Verarbeitungsverzeichnis, DSFA und Betriebsrat

- **Admin ≠ Leser:** Admins verwalten die Instanz, sehen aber keine fremden Mail-Inhalte, nur
  Metadaten und aggregierte Statistiken (Konzept in [`PRIVACY.md`](PRIVACY.md); umgesetzt mit den
  Admin-Funktionen #18, #33, #35).
- **Technischer Zugriff:** Wer Root-Zugriff auf den Host oder die Datenbank hat, kann alle Daten
  lesen. Diesen Personenkreis klein halten und im Berechtigungskonzept festhalten. Für Daten im
  Ruhezustand ein verschlüsseltes Dateisystem bzw. Volume verwenden.
- **Betriebsrat / Mitarbeiterüberwachung:** ollamail bietet keine Funktionen zur Leistungs- oder
  Verhaltenskontrolle. Admin-Statistiken sind aggregiert und nicht personenbezogen auswertbar.
  Da die Software dennoch Daten von Beschäftigten verarbeitet, sollte der Betriebsrat vor der
  Einführung beteiligt werden (in Deutschland u. a. § 87 Abs. 1 Nr. 6 BetrVG).
- **Betroffene Dritte:** E-Mails enthalten personenbezogene Daten der Kommunikationspartner. Der
  Umfang lässt sich über Postfach- und Ordnerauswahl und den Erstimport-Zeitraum begrenzen
  (`OLLAMAIL_MAIL_INITIAL_SYNC_DAYS`, Standard 90 Tage; wirksam ab #14).

## 10. Troubleshooting

Erste Schritte bei jedem Problem:

```sh
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail=100 <dienst>
curl http://localhost:8080/api/readyz
```

| Symptom | Ursache und Lösung |
|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` | `deploy/.env` fehlt oder `POSTGRES_PASSWORD` ist leer. Abschnitt 2.2. |
| `service "migrate" didn't complete successfully` | Ursache in `logs migrate`. Häufig: siehe nächste Zeile. |
| `password authentication failed for user "ollamail"` (in `logs migrate`/`api`) | `POSTGRES_PASSWORD` wurde nach dem ersten Start geändert. Altes Passwort wieder eintragen oder das Passwort in der Datenbank anpassen: `docker compose -f deploy/compose.yaml exec postgres psql -U ollamail -d ollamail -c "ALTER USER ollamail PASSWORD '<neu>'"` (Nutzer/DB-Name ggf. an `POSTGRES_USER`/`POSTGRES_DB` anpassen). |
| `/api/readyz` liefert `503` mit `"database":"failed"` | Datenbank nicht erreichbar. `logs postgres` und `OLLAMAIL_DATABASE_URL` prüfen (Sonderzeichen im Passwort URL-kodieren). |
| `migrate` zeigt `Exited (0)` | Normal: einmaliger Migrationslauf. |
| `worker` startet nicht / `No module named 'app.worker'` | Der Worker existiert noch nicht (#7). `--profile worker` weglassen. |
| `toomanyrequests` / `429 Too Many Requests` beim Build oder Pull | Rate-Limit von Docker Hub. Mit `docker login` anmelden oder später erneut versuchen. |
| `failed to bind host port … address already in use` oder `port is already allocated` | Port 8080 ist belegt. `OLLAMAIL_HTTP_PORT` in `deploy/.env` ändern. |
| UI lädt hinter dem Reverse Proxy, Live-Updates bleiben aus | Pufferung im äußeren Proxy aktiv. Abschnitt 4 (SSE). |
| `could not select device driver "nvidia"` | NVIDIA Container Toolkit fehlt oder Docker wurde danach nicht neu gestartet. Abschnitt 3.3. |
| Container meldet `Read-only file system` | Gewollt: Root-Dateisystem ist schreibgeschützt, beschreibbar sind nur `/tmp` und `/data`. |

Für ausführlichere Logs vorübergehend `OLLAMAIL_LOG_LEVEL=DEBUG` setzen und
`docker compose -f deploy/compose.yaml up -d` ausführen. Auch dann enthalten die Logs keine
Mail-Inhalte.
