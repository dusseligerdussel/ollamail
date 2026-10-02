# CLAUDE.md – Arbeitsregeln für ollamail

Diese Datei gilt für **jeden** Agenten und jeden Menschen, der an diesem Repository arbeitet.
Lies sie vollständig, bevor du etwas änderst. Danach: `docs/ARCHITECTURE.md`, `docs/PRIVACY.md`
und – bei UI-Arbeit – `docs/DESIGN.md`.

## 1. Git-Workflow (verbindlich)

> **Wichtig:** Das Repository ist privat. GitHub setzt Branch-Rulesets für private Repos im
> Free-Plan **nicht** durch. `main` ist also technisch **nicht** geschützt. Die folgenden Regeln
> gelten trotzdem uneingeschränkt und müssen von jedem Agenten selbst eingehalten werden.

- **Niemals direkt auf `main` committen oder pushen** – auch nicht „nur kurz“, auch nicht für Doku.
- Vor jedem Commit prüfen: `git branch --show-current` darf **nicht** `main` sein.
- Jede Änderung – auch Doku, Tippfehler, Konfiguration – passiert auf einem **eigenen Branch**.
- Änderungen gelangen **ausschließlich über einen Pull Request** nach `main`.
- Ein PR wird erst gemerged, wenn die **CI grün** ist und ein Review erfolgt ist.
  Maßgeblich ist der Check **`ci-ok`** (`.github/workflows/ci.yml`); er ist der einzige Required Check
  für eine spätere Branch-Protection.
- **Agenten mergen keine PRs** (weder eigene noch fremde) – das Mergen macht der Repository-Owner,
  außer er beauftragt einen Agenten ausdrücklich damit.
- Kein Force-Push auf `main`, kein Löschen von `main`. Kein Umschreiben fremder Branch-Historie.
- Pushen nur den eigenen Branch: `git push -u origin <eigener-branch>` – nie `git push origin main`,
  nie `git push origin HEAD:main`, nie `git push --all`.

### Technische Absicherung (Ersatz für Branch-Protection)

- `.githooks/pre-push` blockiert jeden Push auf `main` (Löschen und Force-Push eingeschlossen).
- `.claude/settings.json` aktiviert diesen Hook bei jedem Session-Start automatisch
  (`git config core.hooksPath .githooks`). Menschen führen den Befehl einmalig selbst aus.
- Den Hook **niemals** umgehen (`--no-verify`, `core.hooksPath` ändern, Hook löschen/editieren).

### Branch-Namen

```
<typ>/<issue-nummer>-<kurzbeschreibung>
```

Typen: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `ci`.
Beispiele: `feat/12-imap-sync`, `fix/40-token-refresh`, `docs/3-privacy-concept`.
Wenn dir ein Branch-Name vorgegeben wird (z. B. von der Agent-Umgebung), nutze diesen.

### Ablauf für ein Issue

1. Issue lesen, inkl. „Abhängigkeiten“. Sind Abhängigkeiten noch offen → nicht anfangen, sondern melden.
2. Aktuellen `main` holen, Branch davon abzweigen.
3. Klein und fokussiert arbeiten: **ein Issue = ein PR**. Kein Scope-Creep; Zusatzfunde als neues Issue vorschlagen.
4. Lokal Lint, Typecheck und Tests laufen lassen (siehe Abschnitt 4).
5. PR gegen `main` öffnen, Template ausfüllen, im Body `Closes #<nr>` angeben.

### Commits

[Conventional Commits](https://www.conventionalcommits.org/), Sprache Englisch:
`feat(mail): add IMAP IDLE listener`, `fix(auth): refresh expired OIDC token`.

## 2. Parallel arbeitende Agenten

Mehrere Agenten arbeiten gleichzeitig an verschiedenen Issues. Damit das konfliktfrei klappt:

- Bleib in den Verzeichnissen/Modulen, die dein Issue betrifft.
- **Datenbank-Migrationen**: Alembic-Revisionen immer mit `--autogenerate` relativ zum aktuellen
  `main` erzeugen. Bei Konflikten (mehrere Heads) vor dem Merge rebasen bzw. eine Merge-Revision anlegen.
- **Gemeinsame Dateien** (z. B. `backend/app/main.py`, Router-Registrierung, `frontend/src/routes`,
  `compose.yaml`): nur minimale, additive Änderungen.
- **API-Verträge**: Das Backend ist die Quelle der Wahrheit (OpenAPI). Der Frontend-Client wird
  generiert (`pnpm gen:api`), nie von Hand geschrieben.
- Feature-Flags/Settings neuer Module werden in `backend/app/core/config.py` additiv ergänzt.

## 3. Architektur & Stack (Kurzfassung)

Details: `docs/ARCHITECTURE.md`.

| Bereich | Technologie |
|---|---|
| Backend | Python 3.12+, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2 |
| Datenbank | PostgreSQL 16 + pgvector (auch Volltext und Job-Queue) |
| Hintergrundjobs | Procrastinate (Postgres-basierte Queue) – eigener `worker`-Container |
| LLM | Provider-Abstraktion; Standard: Ollama; zusätzlich OpenAI-kompatible Endpunkte; Cloud optional (vom Admin abschaltbar) |
| TTS | Piper (Standard, CPU-effizient, DE/EN), Engine austauschbar |
| Auth | Lokale Accounts (nur Erst-Admin/Fallback), OIDC (Entra ID, GitHub, Google, generisch), LDAP/AD |
| Frontend | React 19, Vite, TypeScript (strict), Tailwind CSS v4, shadcn/ui, TanStack Router + Query |
| Tooling | Backend: `uv`, `ruff`, `mypy`, `pytest`. Frontend: `pnpm`, Biome, Vitest, Playwright |
| Deployment | Docker Compose (primär), Multi-Arch-Images (amd64/arm64) über GHCR; Helm später |

Verzeichnisstruktur (Monorepo):

```
backend/    FastAPI-App, Worker, Alembic
frontend/   React-App
deploy/     compose.yaml, .env.example, später Helm-Chart
docs/       Architektur, Datenschutz, Design, Roadmap
```

## 4. Qualitätsregeln / Definition of Done

Ein Issue ist erst fertig, wenn:

- [ ] Code läuft lokal via Docker Compose.
- [ ] Backend: `uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest` grün.
- [ ] Frontend: `pnpm lint && pnpm typecheck && pnpm test` grün.
- [ ] E2E: `pnpm e2e` grün (gemockte Specs; neue Abläufe bekommen eine Spec). Die CI führt die
      Suite zusätzlich gegen den echten Stack aus (Job „E2E“, siehe `frontend/README.md`).
- [ ] Bei Änderungen an `deploy/` oder den Dockerfiles: Job „Compose smoke test“ grün (läuft
      automatisch; für andere PRs per Label `ci:compose`).
- [ ] Neue Logik hat Tests (Unit; bei API-Endpunkten mindestens ein Integrationstest).
- [ ] Neue Settings sind in `deploy/.env.example` dokumentiert.
- [ ] Datenschutz-Check aus `docs/PRIVACY.md` bedacht (keine Mail-Inhalte in Logs, Verschlüsselung von Secrets, Löschbarkeit).
- [ ] Relevante Doku in `docs/` aktualisiert.
- [ ] PR-Template vollständig ausgefüllt.
- [ ] Bei UI-Änderungen: Screenshots im PR (siehe 4a).

### 4a. Screenshots bei UI-Änderungen (Pflicht)

Jeder PR, der sichtbar etwas an der UI ändert – neue Seiten, neue oder geänderte Komponenten,
Layout, Farben, Texte, Zustände –, **muss Screenshots in der PR-Beschreibung enthalten**.
Ohne Screenshots ist ein UI-PR nicht fertig.

**Was:**
- Jede neue oder geänderte Seite/Ansicht, jeweils in **Light und Dark**.
- **Desktop (1440 × 900)** und **Mobil (390 × 844)**.
- Relevante Zustände: leer, mit Daten, Laden (Skeleton), Fehler, geöffnete Dialoge/Command Palette.
- Bei Änderungen an Bestehendem: **Vorher / Nachher** (Vorher-Screenshots vom aktuellen `main`).
- Nur Testdaten verwenden – **keine echten E-Mails, Namen oder Adressen** (`docs/PRIVACY.md`).
- Dateinamen sprechend: `inbox-desktop-dark.png`, `settings-mobile-light-before.png`.

**Wie:**
1. App starten (`pnpm build && pnpm preview` bzw. Docker Compose) und Screenshots mit Playwright
   erzeugen – in ein temporäres Verzeichnis **außerhalb des Repos**. Falls Playwright keinen Browser
   findet: `chromium.launch({ executablePath: process.env.CHROMIUM_PATH ?? '/opt/pw-browsers/chromium' })`
   (kein `playwright install` in Agent-Umgebungen).
2. Screenshots selbst ansehen und kritisch gegen `docs/DESIGN.md` prüfen, bevor sie in den PR kommen.
3. Hochladen: `scripts/pr-screenshots.sh <verzeichnis>`. Das Skript legt die Bilder auf dem
   Orphan-Branch `pr-screenshots` ab (Ordner = Name deines Branches) und gibt fertiges Markdown aus.
4. Dieses Markdown in den Abschnitt „Screenshots“ der PR-Beschreibung einfügen. Nach weiteren
   UI-Änderungen im selben PR: Skript erneut ausführen und den Abschnitt ersetzen.

**Regeln:**
- Screenshots **nie** in den Feature-Branch oder nach `main` committen.
- Der Branch `pr-screenshots` ist die einzige Ausnahme von „nur den eigenen Branch pushen“ – und
  nur über das Skript. Er wird nie gemergt und nicht gelöscht (sonst brechen die Bildlinks).

## 5. Nicht verhandelbare Prinzipien

1. **Local first / self-hosted.** Jede Funktion muss ohne Cloud-Dienst funktionieren. Cloud-LLMs sind
   Opt-in und vom Admin global abschaltbar.
2. **DSGVO by design.** Keine Mail-Inhalte, Betreffzeilen oder Adressen in Logs oder Telemetrie.
   Zugangsdaten/Tokens verschlüsselt. Admins sehen keine fremden Mail-Inhalte.
3. **Hardware-agnostisch.** Alles muss auf CPU-only laufen (ggf. langsamer, kleinere Modelle).
   GPU ist Beschleunigung, keine Voraussetzung. Modelle sind konfigurierbar, nicht hartkodiert.
4. **Provider-Abstraktion.** Mail (IMAP / Microsoft Graph / Gmail API), LLM und TTS werden über
   Interfaces angebunden. Neue Provider dürfen keinen Code in Features erfordern.
5. **Schlichtes UI.** Siehe `docs/DESIGN.md`. Nichts soll „nach KI aussehen“: keine Glitzer-Icons,
   keine Farbverläufe, keine Sparkle-Emojis, kein „✨ AI“.
6. **Keine Secrets im Repo.** Konfiguration ausschließlich über Umgebungsvariablen.

## 6. Sprache

- Code, Bezeichner, Commits, Code-Kommentare: **Englisch**.
- Doku in `docs/`, Issues, PR-Beschreibungen: **Deutsch** (Englisch ist ok).
- UI: i18n von Anfang an (DE + EN), keine hartkodierten UI-Strings.
