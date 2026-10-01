# ollamail backend

FastAPI-API und Procrastinate-Worker (`python -m app.worker`). Architektur: [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md),
Arbeitsregeln: [`CLAUDE.md`](../CLAUDE.md).

## Voraussetzungen

- [uv](https://docs.astral.sh/uv/) (installiert bei Bedarf automatisch Python 3.12, siehe `.python-version`)

Alle Befehle werden im Verzeichnis `backend/` ausgeführt.

## Installieren

```sh
uv sync
```

## Datenbank

PostgreSQL 16 mit pgvector, z. B. per Docker:

```sh
docker run -d --name ollamail-db -p 5432:5432 \
  -e POSTGRES_USER=ollamail -e POSTGRES_PASSWORD=ollamail -e POSTGRES_DB=ollamail \
  pgvector/pgvector:pg16
```

Migrationen (URL aus `OLLAMAIL_DATABASE_URL`, Standard
`postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail`):

```sh
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "add mailbox table"   # neue Revision
```

Neue Model-Module werden in `app/models.py` importiert, damit Autogenerate sie sieht.

## Starten

```sh
uv run uvicorn app.main:app --reload
```

- `GET /healthz` – Liveness, immer `{"status":"ok"}`
- `GET /readyz` – Readiness, 503 wenn eine Prüfung (z. B. DB) fehlschlägt

Konfiguration ausschließlich über `OLLAMAIL_*`-Umgebungsvariablen, siehe
[`deploy/.env.example`](../deploy/.env.example). Für lesbare Logs lokal: `OLLAMAIL_LOG_FORMAT=console`.

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

Tests mit Marker `db` brauchen PostgreSQL mit pgvector unter `OLLAMAIL_TEST_DATABASE_URL`
(Standard: `postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail_test`, wie in der CI).
Ist die DB nicht erreichbar, werden sie mit Hinweis übersprungen; `OLLAMAIL_TEST_REQUIRE_DB=1`
lässt den Lauf stattdessen fehlschlagen. Nur Unit-Tests: `uv run pytest -m "not db"`.

Fixtures (`tests/conftest.py`): `db_session` (Transaktion je Test, wird zurückgerollt),
`client` (App ohne DB-Override), `db_client` (`get_db` nutzt `db_session`).

Alles zusammen (Definition of Done):

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest
```

## Struktur

```
app/
  main.py   App-Factory (create_app)
  models.py Import aller ORM-Modelle (für Alembic)
  core/     config, db, logging (PII-Filter), errors (Problem Details),
            middleware (Request-ID), health (/healthz, /readyz), ids (UUIDv7)
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
migrations/ Alembic-Revisionen
tests/      pytest-Tests
```

## Konventionen für Module

- **Logging:** `from app.core.logging import get_logger`; Event-Namen statisch, Daten als Felder,
  nur IDs: `log.info("message_synced", message_id=msg.id)`. Nie Inhalte in den Event-Text
  formatieren – der PII-Filter sieht nur Feldnamen. Details: `docs/PRIVACY.md`.
- **Fehler:** `raise ProblemError(404, detail="...")` aus `app.core.errors`; Antworten sind
  RFC 9457 Problem Details (`application/problem+json`) inkl. `request_id`.
- **Modelle:** von `app.core.db.Base` erben (`id` UUIDv7, `created_at`, `updated_at`).
  Sessions per Dependency `get_db`; Commits explizit.
- **Readiness:** `register_readiness_check(app, "queue", check)` aus `app.core.health`.
