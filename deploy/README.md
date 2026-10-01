# Deployment mit Docker Compose

## Schnellstart

```sh
cp deploy/.env.example deploy/.env
# deploy/.env anpassen: mindestens OLLAMAIL_SECRET_KEY (openssl rand -base64 32) und POSTGRES_PASSWORD
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
| `worker` | `backend/Dockerfile` | Hintergrundjobs (`python -m app.worker`) – vorerst Profil `worker` |
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

Übergangsweise, bis der Worker (#7) existiert, startet `worker` nur mit `--profile worker`.
Danach entfällt das Profil und der Worker startet immer.

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
`X-Forwarded-*`-Header werden nur von privaten Netzen akzeptiert.

## Sicherheits-Header

Caddy setzt u. a. eine strikte Content-Security-Policy (`default-src 'self'`, keine externen
Quellen), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` und
`Permissions-Policy`. Für SSE ist Buffering deaktiviert (`flush_interval -1`), Timeouts betragen 1 h.
