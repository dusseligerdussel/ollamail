# ollamail – Frontend

React 19, Vite, TypeScript (strict), Tailwind CSS v4, shadcn/ui, TanStack Router (file-based) und
TanStack Query, i18next (de/en). Tooling: pnpm, Biome, Vitest + Testing Library, Playwright.

## Voraussetzungen

- Node.js ≥ 22
- pnpm (Version über `packageManager` in `package.json`, z. B. via `corepack enable`)

## Skripte

| Befehl | Zweck |
|---|---|
| `pnpm dev` | Dev-Server (Vite); `/api` wird an `http://localhost:8000` weitergeleitet |
| `pnpm build` | Typecheck + Produktions-Build nach `dist/` |
| `pnpm lint` | Biome (Lint + Format-Check); `pnpm format` korrigiert automatisch |
| `pnpm typecheck` | TypeScript ohne Emit |
| `pnpm test` | Unit-Tests (Vitest, jsdom) |
| `pnpm e2e` | Playwright-Tests (startet den Dev-Server selbst) |

Für `pnpm e2e` wird ein Chromium benötigt (`pnpm exec playwright install chromium`). Ein bereits
installierter Browser kann über `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` genutzt werden.

## Struktur

```
src/
  routes/          Datei-basierte Routen (TanStack Router); routeTree.gen.ts wird generiert
  components/ui/   shadcn/ui-Komponenten (nur über die shadcn-CLI hinzufügen)
  components/      eigene, wiederverwendbare Komponenten
  i18n/            i18next-Setup und Übersetzungen (locales/de.json, locales/en.json)
  lib/             Hilfsfunktionen
```

- **shadcn/ui:** Komponenten mit `pnpm dlx shadcn@latest add <name>` hinzufügen, nicht nachbauen.
  Die Konfiguration steht in `components.json`.
- **Routen:** `src/routeTree.gen.ts` wird von Vite (`dev`, `build`, `test`) erzeugt und eingecheckt,
  damit der Typecheck ohne vorherigen Build funktioniert.
- **i18n:** Keine hartkodierten UI-Texte. Neue Keys immer in `de.json` und `en.json` anlegen (ein Test
  prüft, dass beide dieselben Keys haben). Die Sprache kommt aus der Nutzerwahl (`localStorage`) oder
  dem Browser; Fallback ist Englisch.
- **Datenschutz:** Keine externen Requests (CDNs, Fonts, Telemetrie). Die Schrift (Inter Variable)
  wird über `@fontsource-variable/inter` mitgebündelt. Der Playwright-Test prüft, dass beim Laden
  keine Anfragen an fremde Origins entstehen.
