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
  routes/                      Datei-basierte Routen (TanStack Router); routeTree.gen.ts wird generiert
  components/ui/               shadcn/ui-Komponenten (nur über die shadcn-CLI hinzufügen)
  components/app-shell/        Navigation (Sidebar / Bottom-Bar), Layout, globale Shortcuts
  components/command-palette/  Command Palette und Command-Registry
  components/shortcuts/        Shortcut-Registry (Provider + Hooks)
  components/                  Basiskomponenten: EmptyState, ListSkeleton, PageHeader, SplitView, …
  hooks/                       useCurrentUser (Platzhalter), useListNavigation, useMediaQuery
  i18n/                        i18next-Setup und Übersetzungen (locales/de.json, locales/en.json)
  lib/                         Theme, Registry, Shortcut-Parser, Hilfsfunktionen
public/                        theme-init.js, sw.js, manifest.webmanifest, Icons
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

## Design-System und App-Shell

Grundlage ist `docs/DESIGN.md`.

- **Design-Tokens** stehen als CSS-Variablen in `src/index.css`: neutrale Zinc-Grautöne, genau eine
  Akzentfarbe (`--brand`, ein gedecktes Blau; `--primary` und `--ring` leiten sich davon ab), Radius
  (`--radius`), Layout-Maße (`h-header`, `h-row`, `h-bottom-bar`) und die UI-Schriftgröße `text-ui`
  (13 px). Optik über diese Tokens ändern, nicht über Ad-hoc-Klassen.
- **Theme:** Hell, Dunkel oder System (`useTheme()`). Die Wahl liegt in `localStorage`
  (`ollamail.theme`). `public/theme-init.js` setzt die Klasse `dark` blockierend vor dem ersten
  Rendern, damit beim Laden nichts flackert. Logik in beiden Dateien synchron halten.
- **Layout:** ab 768 px Sidebar + Inhalt, ab 1024 px Liste und Detail nebeneinander (`SplitView`).
  Spaltenbreiten sind verstellbar und werden pro Seite gespeichert. Darunter Bottom-Bar, Liste und
  Detail gestapelt.
- **Admin-Navigation** hängt an `useCurrentUser().isAdmin`. Der Hook ist ein Platzhalter (liefert
  immer einen Admin), bis die Authentifizierung (#11, #12) ihn ersetzt.

### Tastenkürzel

Ein globaler `keydown`-Listener verteilt Tasten an registrierte Shortcuts. Seiten registrieren eigene
Shortcuts, solange sie gemountet sind; das `?`-Overlay listet automatisch alle aktiven:

```tsx
useShortcut(
  { id: "mail.archive", keys: "e", group: "list", description: t("mail.archive") },
  () => archive(activeId),
);
```

- `keys`: Kombination (`mod+k`, `shift+e`) oder Sequenz (`g i`). `mod` ist ⌘ auf macOS, sonst Strg.
- In Textfeldern und Dialogen greifen nur Shortcuts mit `allowInInput: true`.
- Listen nutzen `useListNavigation({ count, onOpen })` für `j`/`k`/`o`.
- Neue Gruppen in `shortcutGroups` (`src/lib/shortcuts.ts`) und in den Übersetzungen ergänzen.

### Command Palette

`⌘K` / `Strg+K` öffnet die Palette. Seiten fügen Befehle hinzu, solange sie gemountet sind:

```tsx
const commands = useMemo<Command[]>(
  () => [{ id: "mail.archiveAll", label: t("mail.archiveAll"), group: "actions", icon: Archive, run }],
  [t, run],
);
useCommands(commands);
```

### PWA

`public/manifest.webmanifest` und `public/sw.js` (nur im Produktions-Build registriert). Der Service
Worker cacht ausschließlich die App-Shell (HTML, JS, CSS, Schrift, Icons). Anfragen an `/api` und an
fremde Origins gehen immer direkt ans Netz und werden **nie** gecacht – Mail-Inhalte landen so nicht im
Browser-Cache. Nach Änderungen an der Cache-Strategie `CACHE` in `sw.js` hochzählen.

### Tests

- Unit-Tests (Vitest): Theme-Umschaltung inkl. `theme-init.js`, Shortcut-Parser/-Dispatcher,
  Registries, Command Palette, Navigation, mobile Variante.
- E2E (Playwright): axe-Check (WCAG 2.2 AA) aller Seiten in Hell/Dunkel bei 1440 px und 360 px, keine
  horizontale Überbreite, Tastaturbedienung, Theme vor dem App-Bundle, keine externen Requests.

