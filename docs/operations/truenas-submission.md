# TrueNAS-Katalog: Einreichung von ollamail

Arbeitsblatt für den Repository-Owner, um ollamail in den Community-Train von
[`truenas/apps`](https://github.com/truenas/apps) zu bringen. Der Ablauf steht in
[`truenas.md` §8](truenas.md#8-katalog-app-vorbereiten-und-einreichen); hier liegen die fertigen
Bausteine. Die Texte für `truenas/apps` sind auf Englisch, weil dort Englisch üblich ist.

## Bausteine

| Was | Wo |
|---|---|
| App-Ordner (`ix-dev/community/ollamail`) | [`deploy/truenas/app/`](../../deploy/truenas/app) |
| Icon | [`deploy/truenas/icon.svg`](../../deploy/truenas/icon.svg), [`icon.png`](../../deploy/truenas/icon.png) (256 × 256) |
| Lokale Prüfung | `scripts/truenas-app.sh /tmp/ollamail-truenas` |
| Images | `ghcr.io/dusseligerdussel/ollamail-api`, `ollamail-frontend` (öffentlich, amd64/arm64) |

**Screenshots** (nur Testdaten, 2880 × 1800):

- [Posteingang hell](https://github.com/dusseligerdussel/ollamail/blob/2a6671184c3450d154862fdb7b11d891cb091662/claude-stoic-dijkstra-o63erc/inbox-light.png?raw=true) / [dunkel](https://github.com/dusseligerdussel/ollamail/blob/2a6671184c3450d154862fdb7b11d891cb091662/claude-stoic-dijkstra-o63erc/inbox-dark.png?raw=true)
- [Suche mit Antwort](https://github.com/dusseligerdussel/ollamail/blob/2a6671184c3450d154862fdb7b11d891cb091662/claude-stoic-dijkstra-o63erc/search-light.png?raw=true)
- [Digest](https://github.com/dusseligerdussel/ollamail/blob/2a6671184c3450d154862fdb7b11d891cb091662/claude-stoic-dijkstra-o63erc/digest-light.png?raw=true)
- [Aufgaben](https://github.com/dusseligerdussel/ollamail/blob/2a6671184c3450d154862fdb7b11d891cb091662/claude-stoic-dijkstra-o63erc/todos-light.png?raw=true)

Herunterladen und im PR als Anhang hochladen. TrueNAS legt Icon und Screenshots auf
`media.sys.truenas.net` ab; `icon_url` in `item.yaml` zeigt schon auf den erwarteten Pfad,
`screenshots` bleiben leer, bis TrueNAS die URLs einträgt.

## Schritt 1: Issue in `truenas/apps`

Neues Issue unter <https://github.com/truenas/apps/issues/new> (Vorlage für neue Apps, falls
angeboten). Titel und Text:

**Title:** `New app request: ollamail (self-hosted email analysis with local LLM)`

```markdown
### App

**ollamail** – self-hosted, local-first email analysis: triage with categories and priority,
task extraction, hybrid search with cited answers ("ask your inbox") and a daily audio digest.
It connects to IMAP, Microsoft 365 and Gmail mailboxes and runs fully on local hardware with
Ollama (CPU-only works; GPU optional). Cloud LLMs are opt-in and can be disabled by the admin.

- Source: https://github.com/dusseligerdussel/ollamail (AGPL-3.0)
- Images: ghcr.io/dusseligerdussel/ollamail-api, ghcr.io/dusseligerdussel/ollamail-frontend
  (public, multi-arch amd64/arm64, semver tags, SBOM + SLSA provenance, Trivy-scanned)
- Latest release: https://github.com/dusseligerdussel/ollamail/releases/latest

### Proposed catalog app

An app for the community train is ready: ix_lib 2.3.15, PostgreSQL via `tpl.deps.postgres`,
optional bundled Ollama (CPU or NVIDIA GPU) or an external Ollama/OpenAI-compatible endpoint,
ixVolume or host path storage. It passes `apps_validation`, renders with both test value files,
and our CI starts the rendered app and waits for all containers to be healthy.

Two deliberate deviations we would like to confirm before opening the PR:

1. **Fixed UID/GID 10001** for api/worker/frontend instead of a selectable 568 – the images ship
   their user and file ownership; a `permissions` step chowns the data volume.
2. **`internal: true` on the `backend` network** (PostgreSQL, Ollama) so they have no route to
   the outside, matching the upstream Compose stack. The template sets this after rendering,
   because the library has no option for internal networks.

Default port: 30580 (free in the catalog when checked).

Would you accept this app into the community train? Happy to adjust anything.
```

## Schritt 2: Pull Request

Erst nach positiver Antwort im Issue. Fork von `truenas/apps`, dann im Fork:

```sh
cp -r <ollamail>/deploy/truenas/app ix-dev/community/ollamail
devbox shell
devbox run copy-lib
./.github/scripts/generate_metadata.py
./.github/scripts/port_validation.py
./.github/scripts/ci.py --app ollamail --train community --test-file basic-values.yaml
./.github/scripts/ci.py --app ollamail --train community --test-file bundled-ollama-values.yaml
```

PR mit der Vorlage `app_addition.md` öffnen, deren Abschnitte ausfüllen und die folgenden
Bausteine übernehmen. Icon und Screenshots anhängen.

**Title:** `Add ollamail to community train`

```markdown
## Description

Adds **ollamail** (https://github.com/dusseligerdussel/ollamail), a self-hosted email analysis
app with a local LLM: triage, task extraction, cited search answers and a daily audio digest for
IMAP, Microsoft 365 and Gmail mailboxes. Discussed in #<issue-number>.

- Images: ghcr.io/dusseligerdussel/ollamail-api / ollamail-frontend (public, amd64/arm64, semver)
- Services: migrate (one-shot), api, worker, frontend, PostgreSQL (`tpl.deps.postgres`),
  optional bundled Ollama (CPU or NVIDIA GPU)
- Storage: data + postgres (+ ollama models when bundled), ixVolume or host path
- Port: 30580 (web UI)

### Deviations from the usual conventions

1. Fixed UID/GID 10001 for api/worker/frontend: the images define user and ownership.
   The `permissions` container prepares the data volume.
2. The `backend` network is `internal: true` (PostgreSQL/Ollama without outbound route),
   applied after rendering because ix_lib has no internal-network option.

## Testing

<!-- Nur eintragen, was im Fork tatsächlich gelaufen ist. -->
- `apps_validation`, `generate_metadata.py`, `port_validation.py`: pass
- `ci.py --app ollamail --train community` with `basic-values.yaml` and
  `bundled-ollama-values.yaml`: all containers healthy

## Screenshots / Icon

Attached (test data only).
```

Im Feld bzw. bei der Frage „AI / LLM generated“: **ja** ankreuzen. Die App-Dateien wurden
mit Unterstützung von Claude Code erstellt und vom Owner geprüft.

## Nach der Aufnahme

- Die App wird im Katalog gepflegt. Neue Releases als PR dort nachziehen: Tag in `ix_values.yaml`,
  `app_version` und `version` in `app.yaml` erhöhen. Renovate meldet neue Image-Tags
  (SemVer, keine Zusatzregel nötig).
- Änderungen an `deploy/truenas/app/` hier und dort gleich halten.
