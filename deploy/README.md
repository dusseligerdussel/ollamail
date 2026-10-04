# Deployment mit Docker Compose

Referenz für den Compose-Stack. Schritt-für-Schritt-Installation, Reverse-Proxy-Beispiele,
Backup/Restore, Updates und Fehlersuche: [`docs/OPERATIONS.md`](../docs/OPERATIONS.md).
Für Kubernetes gibt es ein Helm-Chart unter [`helm/ollamail`](helm/ollamail), siehe
[`docs/operations/kubernetes.md`](../docs/operations/kubernetes.md).

## Schnellstart

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# deploy/.env anpassen: mindestens OLLAMAIL_SECRET_KEY (openssl rand -base64 32) und
# POSTGRES_PASSWORD (openssl rand -hex 24)
docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml up -d --build
```

Das baut die Images lokal. Mit Zugriff auf die fertigen Images aus GHCR (siehe [Images](#images))
reicht stattdessen:

```sh
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d
```

`compose.yaml` allein enthält keine Build-Kontexte: `up -d --build` ohne `compose.build.yaml`
versucht die GHCR-Images zu ziehen und bricht ohne Zugriff mit `unauthorized` ab.

Die UI ist danach unter <http://localhost:8080> erreichbar, die API unter `/api`
(z. B. `curl http://localhost:8080/api/healthz`). Der Port ist standardmäßig nur an `127.0.0.1`
gebunden; für Zugriff aus dem LAN `OLLAMAIL_HTTP_BIND` bewusst setzen (siehe
[TLS / Reverse Proxy](#tls--reverse-proxy)).

Alle Variablen sind in [`.env.example`](.env.example) beschrieben.

## Dienste

| Dienst | Image | Aufgabe |
|---|---|---|
| `frontend` | `ollamail-frontend` (`frontend/Dockerfile`, Caddy) | Statische UI, Reverse Proxy `/api/*` → `api:8000` (Präfix wird entfernt), einziger veröffentlichter Port |
| `api` | `ollamail-api` (`backend/Dockerfile`) | FastAPI (uvicorn) |
| `worker` | `ollamail-api` | Hintergrundjobs (`python -m app.worker`, Procrastinate), Queues `sync`, `llm`, `tts`, `ocr`, `default`, `push`; Healthcheck über eine Heartbeat-Datei (`python -m app.core.heartbeat`) |
| `migrate` | `ollamail-api` | One-Shot `alembic upgrade head` vor jedem Start von `api`/`worker` |
| `postgres` | `pgvector/pgvector:0.8.7-pg16-bookworm` | Datenbank, Volume `postgres-data`; Tuning für pgvector über `POSTGRES_*` ([`OPERATIONS.md` §8.4](../docs/OPERATIONS.md#84-postgresql-tuning-pgvector)) |
| `ollama-cpu` / `ollama-gpu` | `ollama/ollama:0.35.0` | Optionaler LLM-Server, im Netz `backend` als `ollama` erreichbar |

Volumes: `postgres-data` (Datenbank), `ollamail-data` (Anhänge, Audio; `/data` in `api`/`worker`),
`ollama-models` (Modelle).

`api`, `worker` und `frontend` laufen als unprivilegierter Nutzer (UID 10001), mit schreibgeschütztem
Dateisystem (nur `/tmp` und `/data` beschreibbar), ohne Linux-Capabilities und mit `no-new-privileges`.
Ollama läuft im Upstream-Image als root, aber ebenfalls mit schreibgeschütztem Dateisystem (nur
`/tmp` und das Modell-Volume), ohne Capabilities und mit `no-new-privileges`; ohne Capabilities darf
dieser root nur eigene Dateien schreiben und keine fremden Rechte übernehmen.

Die Drittanbieter-Images sind auf feste Versionen gepinnt (PostgreSQL inkl. Debian-Release, damit
sich die Collations einer bestehenden Datenbank nicht unbemerkt ändern). Updates schlägt Dependabot
vor; Wechsel siehe [`OPERATIONS.md` §6.5](../docs/OPERATIONS.md#65-drittanbieter-images).

## Netze

Der Stack nutzt drei Netze statt eines gemeinsamen:

| Netz | Mitglieder | Zweck |
|---|---|---|
| `edge` | `frontend`, `api` | Einziger Weg vom veröffentlichten Port nach innen: Caddy erreicht nur die API |
| `backend` (`internal: true`) | `api`, `worker`, `migrate`, `postgres`, Ollama | Datenbank und LLM; ohne Route nach außen |
| `egress` | `api`, `worker`, Ollama | Ausgehender Verkehr: Mailserver, Identity-Provider, LLM-Endpunkte, TTS-Stimmen, Modell-Downloads |

Damit gilt:

- `frontend` kann `postgres` und `ollama` weder auflösen noch erreichen.
- `postgres` und `migrate` haben keinen Zugang nach außen und sind von außen nicht erreichbar.
- Ollama (ohne eigene Authentifizierung) ist nur für `api` und `worker` erreichbar.

**Ollama ohne Internet:** `egress` braucht Ollama nur für Modell-Downloads (`ollama pull`, der Button
auf der Admin-Seite, `OLLAMAIL_LLM_PULL_MISSING_MODELS`). Sind alle Modelle geladen, lässt sich der
Zugang mit einer eigenen Override-Datei (z. B. `deploy/compose.override.yaml`, nicht im Repository)
entfernen:

```yaml
services:
  ollama-cpu:          # bzw. ollama-gpu
    networks: !override
      backend:
        aliases: [ollama]
```

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.override.yaml --profile ollama-cpu up -d
```

Neue Modelle dann vorübergehend ohne diese Datei laden; dazu
`OLLAMAIL_LLM_PULL_MISSING_MODELS=false` setzen (sonst schlägt der automatische Download fehl).

**Eigene Container anbinden:** Prometheus (Scrape von `api:8000` und `worker:9464`) oder ein
externer Ollama-Container hängen sich an das Netz `ollamail_backend` (Compose-Projektname
`ollamail` + Netzname), ein Reverse Proxy als Container an `ollamail_edge` – nie an beide, wenn er
von außen erreichbar ist.

Upgrade bestehender Installationen: `docker compose -f deploy/compose.yaml up -d` legt die neuen
Netze an und verbindet die Container neu; das alte Netz `ollamail_default` bleibt ungenutzt übrig
und lässt sich mit `docker network rm ollamail_default` entfernen. Wer eigene Container an
`ollamail_default` gehängt hatte, verbindet sie wie oben beschrieben neu.

## Images

Die Release-Pipeline ([`.github/workflows/release.yml`](../.github/workflows/release.yml)) baut beide
Images für `linux/amd64` und `linux/arm64` (z. B. Raspberry Pi 4/5, Apple Silicon, Ampere) und
veröffentlicht sie in der GitHub Container Registry:

- `ghcr.io/dusseligerdussel/ollamail-api` – Backend (`api`, `worker`, `migrate`)
- `ghcr.io/dusseligerdussel/ollamail-frontend` – UI und Reverse Proxy

Jede Plattform wird nativ auf einem eigenen Runner gebaut (arm64 auf `ubuntu-24.04-arm`, ohne
QEMU-Emulation) und per Digest gepusht; ein Merge-Job pro Image erzeugt daraus das
Multi-Arch-Manifest und setzt die Tags. Ein manueller Lauf (*Actions → Release → Run workflow*)
ohne `push` baut und prüft nur.

| Tag | Quelle |
|---|---|
| `1.2.3`, `1.2`, `1` | Git-Tag `v1.2.3` (`1` erst ab Version 1.0) |
| `latest` | Neuestes stabiles Release (nicht bei Pre-Releases wie `v1.0.0-rc.1`) |
| `0.0.1-test` | Pre-Release-Tag `v0.0.1-test` (nur dieser Tag) |
| `edge` | Nächtlicher Build von `main` (nur wenn sich etwas geändert hat) – ungetestet, nicht für den Produktivbetrieb |
| `sha-<commit>` | Jeder Build, unveränderlich |

Die Version wählt `OLLAMAIL_VERSION` in `deploy/.env` (Standard `latest`). `latest` gibt es erst
ab dem ersten stabilen Release (`v0.1.0`); bis dahin nur `edge` und `sha-<commit>`. Für
reproduzierbare Installationen eine feste Version eintragen, z. B. `OLLAMAIL_VERSION=0.1.0`.
Update (Backup vorher, siehe [`docs/OPERATIONS.md` §6](../docs/OPERATIONS.md#6-updates-und-migrationen)):

```sh
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d
```

Jedes Image enthält eine SBOM und eine SLSA-Provenance-Attestation (BuildKit) und wird vor dem
Veröffentlichen mit Trivy geprüft; behebbare CVEs der Stufen HIGH und CRITICAL brechen den Build ab
(akzeptierte Ausnahmen mit Begründung in [`.trivyignore`](../.trivyignore)). Anzeigen z. B. mit
`docker buildx imagetools inspect ghcr.io/dusseligerdussel/ollamail-api:<tag> --format '{{ json .SBOM }}'`.

### Zugriff auf die Images

Das Repository ist öffentlich, die GHCR-Pakete sind es derzeit noch **nicht**: Pakete behalten
die Sichtbarkeit, mit der sie angelegt wurden (damals privat), auch wenn das Repository später
öffentlich wird. Solange das so ist, ist zum Ziehen ein Login mit einem Personal Access Token
(classic) mit dem Scope `read:packages` und Lesezugriff auf die Pakete nötig:

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-benutzer> --password-stdin
```

Ohne Zugriff auf die Pakete die Images lokal bauen (`compose.build.yaml`, siehe Schnellstart).
Ob die Pakete öffentlich werden, entscheidet der Repository-Owner (GitHub → Packages →
`ollamail-api` bzw. `ollamail-frontend` → Package settings → Change visibility); danach entfällt
der Login.

### Lokaler Build

[`compose.build.yaml`](compose.build.yaml) ergänzt die Build-Kontexte und taggt die Images lokal
als `ollamail-api:local` bzw. `ollamail-frontend:local`; `OLLAMAIL_VERSION` und
`OLLAMAIL_*_IMAGE` werden dann ignoriert. Die Entwicklungsumgebung (`compose.dev.yaml`) baut
ebenfalls immer lokal.

## Profile

```sh
# Ollama auf CPU
docker compose -f deploy/compose.yaml --profile ollama-cpu up -d
# Ollama mit NVIDIA-GPU (benötigt das NVIDIA Container Toolkit)
docker compose -f deploy/compose.yaml --profile ollama-gpu up -d
# Modell laden (Beispiel)
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull <modell>
```

Ohne Profil nutzt ollamail einen externen Server: `OLLAMAIL_LLM_BASE_URL` anpassen.

## Worker skalieren

Der `worker` startet immer mit. Welche Queues er abarbeitet und wie parallel, steuern
`OLLAMAIL_WORKER_QUEUES`, `OLLAMAIL_WORKER_CONCURRENCY`, `OLLAMAIL_LLM_MAX_CONCURRENCY` und
`OLLAMAIL_LLM_CONCURRENCY` (siehe `.env.example`; letztere lässt sich im Admin-Bereich unter „KI“
zur Laufzeit ändern). Mehr Instanzen: `docker compose -f deploy/compose.yaml up -d --scale worker=2`.
Beim Stoppen bekommen laufende Jobs `OLLAMAIL_WORKER_SHUTDOWN_TIMEOUT` Sekunden (Standard 30),
Compose wartet 45 s, bevor es den Container hart beendet. Jeder Worker braucht mit Standardwerten
bis zu 33 Datenbankverbindungen, die API 21; die mitgelieferte Datenbank erlaubt 200
(`POSTGRES_MAX_CONNECTIONS`, Formel in [`OPERATIONS.md` §8.2](../docs/OPERATIONS.md#82-datenbankverbindungen)).
Prometheus-Metriken (`OLLAMAIL_METRICS_ENABLED`) liefern `api:8000/metrics` und jeder Worker auf
Port 9464, nur im Compose-Netz ([§8.3](../docs/OPERATIONS.md#83-monitoring-prometheus)).

## Entwicklung (Hot Reload)

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up --build
```

- <http://localhost:8080> – UI über den Vite-Dev-Server (HMR), gleiches `/api`-Routing wie in Produktion
- <http://localhost:8000> – API direkt (`uvicorn --reload`, `backend/` ist eingebunden)
- `localhost:5432` – PostgreSQL

## Master-Key und Key-Rotation

`OLLAMAIL_SECRET_KEY` verschlüsselt alle gespeicherten Zugangsdaten (Envelope-Encryption,
AES-256-GCM, siehe `docs/PRIVACY.md`). Ohne gültigen Key (Base64, mindestens 32 zufällige Bytes)
startet `api` nicht. Den Key sicher aufbewahren – ohne ihn sind gespeicherte
Zugangsdaten verloren.

Key wechseln:

1. Bisherigen Key nach `OLLAMAIL_SECRET_KEYS_OLD` verschieben (kommagetrennt), neuen Key als
   `OLLAMAIL_SECRET_KEY` setzen (`openssl rand -base64 32`).
2. Stack neu starten – alte Werte bleiben lesbar, neue werden mit dem neuen Key verschlüsselt.
3. `docker compose -f deploy/compose.yaml run --rm api python -m app.cli rotate-keys`
   verschlüsselt alle gespeicherten Secrets mit dem neuen Key (in einer Transaktion).
4. `OLLAMAIL_SECRET_KEYS_OLD` leeren und neu starten.

Wiederherstellungscodes für die Zwei-Faktor-Anmeldung sind nur als HMAC gespeichert und lassen
sich nicht umschlüsseln. Sie funktionieren nach der Rotation weiter, solange der alte Key in
`OLLAMAIL_SECRET_KEYS_OLD` steht; nach Schritt 4 nicht mehr. Vor Schritt 4 die Nutzer bitten,
neue Codes zu erzeugen, oder den alten Key dort belassen (`docs/auth/mfa.md`).

## TLS / Reverse Proxy

Der `frontend`-Container spricht nur HTTP und ist standardmäßig nur unter `127.0.0.1` veröffentlicht
(`OLLAMAIL_HTTP_BIND`). Für den Betrieb im Netz einen TLS-terminierenden Reverse Proxy (z. B. Caddy,
Traefik, nginx) auf demselben Host davorsetzen. Direkter Zugriff aus dem LAN (unverschlüsseltes HTTP)
nur bewusst: `OLLAMAIL_HTTP_BIND` auf die LAN-Adresse des Hosts oder `0.0.0.0` setzen.
Konfigurationsbeispiele: [`docs/OPERATIONS.md`](../docs/OPERATIONS.md#4-reverse-proxy-und-tls).
`X-Forwarded-*`-Header werden nur von privaten Netzen akzeptiert; der äußere Proxy muss
`X-Forwarded-For` auf die echte Client-IP setzen statt anzuhängen (Rate-Limits).

**HSTS ist Pflicht im äußeren Proxy.** Weil TLS dort endet, setzt der interne Caddy standardmäßig
keinen `Strict-Transport-Security`-Header. Der äußere Proxy muss ihn senden, z. B.
`max-age=31536000; includeSubDomains` (Beispiele für Caddy, nginx und Traefik:
[`OPERATIONS.md` §4.4](../docs/OPERATIONS.md#44-sicherheits-header)). Kann der Proxy keine Header
setzen, sendet der `frontend`-Container den Header mit `OLLAMAIL_HSTS` in `deploy/.env`
(z. B. `OLLAMAIL_HSTS=max-age=31536000; includeSubDomains`) – nur bei Zugriff ausschließlich über
HTTPS, sonst sperren Browser den Host für die angegebene Dauer.

## Sicherheits-Header

Caddy setzt u. a. eine strikte Content-Security-Policy (`default-src 'self'`, keine externen
Quellen), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` und
`Permissions-Policy`; `Strict-Transport-Security` nur mit `OLLAMAIL_HSTS` (siehe oben). Für SSE ist Buffering deaktiviert (`flush_interval -1`), Timeouts betragen 1 h.

## Smoke-Test in der CI

Der Job „Compose smoke test“ (`.github/workflows/ci.yml`) baut beide Images, erzeugt `deploy/.env`
aus `.env.example` mit frisch generierten Secrets und startet den Stack wie oben (ohne Ollama). Er
prüft `/api/readyz`, `/api/healthz`, die Auslieferung der UI, die Migrationen und den Worker
(Heartbeat, Healthcheck, keine Neustarts), dass `/metrics` nur im Compose-Netz und nur mit Token
erreichbar ist, die Netztrennung (`frontend` erreicht nur `api`, `postgres` ist von `api` aus
erreichbar, aber ohne Zugang nach außen), sowie, dass `api` ohne `OLLAMAIL_SECRET_KEY` nicht startet. Er läuft auf
`main`, bei Änderungen an `deploy/`, den Dockerfiles oder der Caddy-Konfiguration und auf PRs mit dem
Label `ci:compose`. Bei Fehlern werden die Container-Logs (ohne Umgebungsvariablen, Secrets geschwärzt)
als Artifact hochgeladen.
