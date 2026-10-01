# ollamail backend

FastAPI-API und (später) Procrastinate-Worker. Architektur: [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md),
Arbeitsregeln: [`CLAUDE.md`](../CLAUDE.md).

## Voraussetzungen

- [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf automatisch Python 3.12, siehe `.python-version`)

Alle Befehle werden im Verzeichnis `backend/` ausgeführt.

## Installieren

```sh
uv sync
```

## Starten

```sh
uv run uvicorn app.main:app --reload
```

Health-Check: `curl http://127.0.0.1:8000/healthz` → `{"status":"ok"}`

## Lint, Format, Typecheck

```sh
uv run ruff check .
uv run ruff format --check .   # ohne --check: formatiert
uv run mypy app
```

## Tests

```sh
uv run pytest
```

Alles zusammen (Definition of Done):

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest
```

## Struktur

```
app/
  main.py   App-Factory (create_app) und /healthz
  core/     Konfiguration, Logging, DB, Sicherheit
  auth/     lokale Accounts, OIDC, LDAP
  users/    Nutzer, Gruppen, Rollen
  mail/     Mail-Provider und Sync
  ai/       LLM-Provider, Embeddings, Prompts
  triage/   Klassifikation
  todos/    Aufgaben-Extraktion
  digest/   Tageszusammenfassung, TTS
  rag/      Retrieval und Chat
  admin/    Instanz-Einstellungen
  audit/    Audit-Events
tests/      pytest-Tests
```
