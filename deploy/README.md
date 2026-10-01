# Deployment mit Docker Compose

## Schnellstart

```sh
cp deploy/.env.example deploy/.env
# deploy/.env anpassen: mindestens OLLAMAIL_SECRET_KEY und POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
```

Die UI ist danach unter <http://localhost:8080> erreichbar, die API unter `/api`
(z. B. `curl http://localhost:8080/api/healthz`).

Alle Variablen sind in [`.env.example`](.env.example) beschrieben.

## Dienste

| Dienst | Image | Aufgabe |
|---|---|---|
| `frontend` | `frontend/Dockerfile` (Caddy) | Statische UI, Reverse Proxy `/api/*` → `api:8000` (Präfix wird entfernt), einziger veröffentlichter Port |
| `api` | `backend/Dockerfile` | FastAPI (uvicorn) |
| `worker` | `backend/Dockerfile` | Hintergrundjobs (`python -m app.worker`, Procrastinate), Queues `sync`, `llm`, `tts`, `default` |
| `migrate` | `backend/Dockerfile` | One-Shot `alembic upgrade head` vor jedem Start von `api`/`worker` |
| `postgres` | `pgvector/pgvector:pg16` | Datenbank, Volume `postgres-data` |
| `ollama-cpu` / `ollama-gpu` | `ollama/ollama` | Optionaler LLM-Server, im Netz als `ollama` erreichbar |

Volumes: `postgres-data` (Datenbank), `ollamail-data` (Anhänge, Audio; `/data` in `api`/`worker`),
`ollama-models` (Modelle).

`api`, `worker` und `frontend` laufen als unprivilegierter Nutzer (UID 10001), mit schreibgeschütztem
Dateisystem (nur `/tmp` und `/data` beschreibbar), ohne Linux-Capabilities und mit `no-new-privileges`.

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

## TLS / Reverse Proxy

Der `frontend`-Container spricht nur HTTP. Für den Betrieb im Netz einen TLS-terminierenden
Reverse Proxy (z. B. Caddy, Traefik, nginx) davorsetzen und `OLLAMAIL_HTTP_BIND=127.0.0.1` setzen.
`X-Forwarded-*`-Header werden nur von privaten Netzen akzeptiert.

## Sicherheits-Header

Caddy setzt u. a. eine strikte Content-Security-Policy (`default-src 'self'`, keine externen
Quellen), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` und
`Permissions-Policy`. Für SSE ist Buffering deaktiviert (`flush_interval -1`), Timeouts betragen 1 h.
