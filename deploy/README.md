# Deployment mit Docker Compose

Referenz für den Compose-Stack. Schritt-für-Schritt-Installation, Reverse-Proxy-Beispiele,
Backup/Restore, Updates und Fehlersuche: [`docs/OPERATIONS.md`](../docs/OPERATIONS.md).

## Schnellstart

```sh
cp deploy/.env.example deploy/.env
# deploy/.env anpassen: mindestens OLLAMAIL_SECRET_KEY (openssl rand -base64 32) und POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d
```

Standardmäßig werden die fertigen Images aus GHCR gezogen (siehe [Images](#images)). Solange das
Repository privat ist, ist dafür ein `docker login ghcr.io` nötig. Alternativ lokal bauen:

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml up -d --build
```

Die UI ist danach unter <http://localhost:8080> erreichbar, die API unter `/api`
(z. B. `curl http://localhost:8080/api/healthz`).

Alle Variablen sind in [`.env.example`](.env.example) beschrieben.

## Dienste

| Dienst | Image | Aufgabe |
|---|---|---|
| `frontend` | `ollamail-frontend` (`frontend/Dockerfile`, Caddy) | Statische UI, Reverse Proxy `/api/*` → `api:8000` (Präfix wird entfernt), einziger veröffentlichter Port |
| `api` | `ollamail-api` (`backend/Dockerfile`) | FastAPI (uvicorn) |
| `worker` | `ollamail-api` | Hintergrundjobs (`python -m app.worker`, Procrastinate), Queues `sync`, `llm`, `tts`, `default` |
| `migrate` | `ollamail-api` | One-Shot `alembic upgrade head` vor jedem Start von `api`/`worker` |
| `postgres` | `pgvector/pgvector:pg16` | Datenbank, Volume `postgres-data` |
| `ollama-cpu` / `ollama-gpu` | `ollama/ollama` | Optionaler LLM-Server, im Netz als `ollama` erreichbar |

Volumes: `postgres-data` (Datenbank), `ollamail-data` (Anhänge, Audio; `/data` in `api`/`worker`),
`ollama-models` (Modelle).

`api`, `worker` und `frontend` laufen als unprivilegierter Nutzer (UID 10001), mit schreibgeschütztem
Dateisystem (nur `/tmp` und `/data` beschreibbar), ohne Linux-Capabilities und mit `no-new-privileges`.

## Images

Die Release-Pipeline ([`.github/workflows/release.yml`](../.github/workflows/release.yml)) baut beide
Images für `linux/amd64` und `linux/arm64` (z. B. Raspberry Pi 4/5, Apple Silicon, Ampere) und
veröffentlicht sie in der GitHub Container Registry:

- `ghcr.io/dusseligerdussel/ollamail-api` – Backend (`api`, `worker`, `migrate`)
- `ghcr.io/dusseligerdussel/ollamail-frontend` – UI und Reverse Proxy

| Tag | Quelle |
|---|---|
| `1.2.3`, `1.2`, `1` | Git-Tag `v1.2.3` (`1` erst ab Version 1.0) |
| `latest` | Neuestes stabiles Release (nicht bei Pre-Releases wie `v1.0.0-rc.1`) |
| `0.0.1-test` | Pre-Release-Tag `v0.0.1-test` (nur dieser Tag) |
| `edge` | Aktueller Stand von `main` – ungetestet, nicht für den Produktivbetrieb |
| `sha-<commit>` | Jeder Build, unveränderlich |

Die Version wählt `OLLAMAIL_VERSION` in `deploy/.env` (Standard `latest`). Für reproduzierbare
Installationen eine feste Version eintragen, z. B. `OLLAMAIL_VERSION=1.2.3`. Update:

```sh
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d
```

Jedes Image enthält eine SBOM und eine SLSA-Provenance-Attestation (BuildKit) und wird vor dem
Veröffentlichen mit Trivy geprüft; behebbare kritische CVEs brechen den Build ab. Anzeigen z. B. mit
`docker buildx imagetools inspect ghcr.io/dusseligerdussel/ollamail-api:<tag> --format '{{ json .SBOM }}'`.

### Zugriff (privates Repository)

Das Repository ist derzeit **privat**. GHCR-Pakete erben diese Sichtbarkeit, die Images sind also
ebenfalls privat. Zum Ziehen ist ein Login mit einem Personal Access Token (classic) mit dem Scope
`read:packages` nötig:

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-benutzer> --password-stdin
```

Ohne Zugriff auf die Pakete die Images lokal bauen (`compose.build.yaml`, siehe Schnellstart).
Ob die Pakete öffentlich werden, entscheidet der Repository-Owner (Paket-Einstellungen in GHCR).

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
`OLLAMAIL_WORKER_QUEUES`, `OLLAMAIL_WORKER_CONCURRENCY` und `OLLAMAIL_LLM_CONCURRENCY`
(siehe `.env.example`). Mehr Instanzen: `docker compose -f deploy/compose.yaml up -d --scale worker=2`.
Beim Stoppen bekommen laufende Jobs `OLLAMAIL_WORKER_SHUTDOWN_TIMEOUT` Sekunden (Standard 30),
Compose wartet 45 s, bevor es den Container hart beendet.

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

## TLS / Reverse Proxy

Der `frontend`-Container spricht nur HTTP. Für den Betrieb im Netz einen TLS-terminierenden
Reverse Proxy (z. B. Caddy, Traefik, nginx) davorsetzen und `OLLAMAIL_HTTP_BIND=127.0.0.1` setzen.
Konfigurationsbeispiele: [`docs/OPERATIONS.md`](../docs/OPERATIONS.md#4-reverse-proxy-und-tls).
`X-Forwarded-*`-Header werden nur von privaten Netzen akzeptiert.

## Sicherheits-Header

Caddy setzt u. a. eine strikte Content-Security-Policy (`default-src 'self'`, keine externen
Quellen), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` und
`Permissions-Policy`. Für SSE ist Buffering deaktiviert (`flush_interval -1`), Timeouts betragen 1 h.
