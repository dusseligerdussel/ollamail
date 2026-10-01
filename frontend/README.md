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
| `pnpm gen:api` | OpenAPI-Schema aus dem Backend exportieren und API-Typen erzeugen (braucht `uv`) |
| `pnpm gen:api:types` | Nur die Typen aus dem eingecheckten `src/api/openapi.json` erzeugen |

Für `pnpm e2e` wird ein Chromium benötigt (`pnpm exec playwright install chromium`). Ein bereits
installierter Browser kann über `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` genutzt werden.

## Struktur

```
src/
  api/                         API-Client, Fehler, Queries; openapi.json + schema.gen.ts werden generiert
  routes/                      Datei-basierte Routen (TanStack Router); routeTree.gen.ts wird generiert
  components/ui/               shadcn/ui-Komponenten (nur über die shadcn-CLI hinzufügen)
  components/app-shell/        Navigation (Sidebar / Bottom-Bar), Layout, globale Shortcuts
  components/command-palette/  Command Palette und Command-Registry
  components/shortcuts/        Shortcut-Registry (Provider + Hooks)
  components/                  Basiskomponenten: EmptyState, ListSkeleton, PageHeader, SplitView, …
  hooks/                       useCurrentUser (Platzhalter), useEvents, useListNavigation, useMediaQuery
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

## API-Client

Das Backend ist die Quelle der Wahrheit. Der Client wird nie von Hand geschrieben:

1. `backend/scripts/export_openapi.py` exportiert das Schema ohne laufenden Server nach
   `src/api/openapi.json` (eingecheckt, Schlüssel sortiert).
2. `openapi-typescript` erzeugt daraus `src/api/schema.gen.ts` (eingecheckt, nicht bearbeiten).

`pnpm gen:api` macht beides. Nach jeder Änderung an Backend-Endpunkten ausführen und das Ergebnis mit
committen; der CI-Job „API client up to date“ schlägt sonst fehl. `openapi-typescript` braucht die
Compiler-API von TypeScript 5, die TypeScript 7 nicht mehr mitliefert; `.pnpmfile.cjs` gibt ihm
deshalb ein eigenes TypeScript 5.

```ts
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";

const health = useQuery({
  queryKey: ["health"],
  queryFn: ({ signal }) => unwrap(api.GET("/healthz", { signal })), // Pfade und Typen aus dem Schema
});
```

- **Basis-URL** `/api` (gleicher Origin, Cookies via `credentials: "include"`). Caddy und der
  Vite-Dev-Proxy entfernen das Präfix, das Backend kennt nur `/healthz` usw.
- **CSRF:** Bei `POST`/`PUT`/`PATCH`/`DELETE` wird der Wert des Cookies `ollamail_csrf` im Header
  `X-CSRF-Token` mitgeschickt (Double-Submit; das Backend setzt das Cookie mit der Auth, #11).
- **Fehler:** `unwrap()` wirft `ApiError` (`status`, `problem` = RFC 9457 Problem Details,
  `requestId`; `status` 0 = Server nicht erreichbar). `describeApiError(error, t)` liefert den
  übersetzten Text. Server-Texte (`detail`) sind englisch und werden nicht angezeigt.
- **Anzeige** (zentral in `src/query-client.ts`): Mutationen → Toast. Queries → inline über
  `<InlineError error={query.error} />`; Toast nur, wenn schon Daten angezeigt werden (fehlgeschlagene
  Aktualisierung). Abweichungen über `meta: { errorToast: true | false }`. 4xx werden nicht wiederholt.
- **401** → Weiterleitung auf `/login?redirect=<aktuelle Seite>` (ganze Seite neu laden), kein Toast.

### Echtzeit-Events

`useEvents()` (in der App-Shell gemountet) abonniert `GET /api/events` per `EventSource` und
übersetzt Events in TanStack-Query-Invalidierungen. Vertrag mit dem Backend (#7):

```
data: {"type":"message.synced","message_id":"…","mailbox_id":"…"}
```

- `data` ist JSON mit `type` (`<ressource>.<aktion>`) und nur IDs/Status, nie Inhalte.
  Alternativ darf der Typ als SSE-`event:`-Name kommen; solche benannten Events empfängt der Browser
  aber nur für Typen, die in `invalidationRules` eingetragen sind. Unbenannte Events bevorzugen.
- Standard: `message.synced` invalidiert alle Queries mit Schlüssel `["message", …]`. Abweichende
  Regeln in `invalidationRules` (`src/hooks/use-events.ts`) nach Event-Typ eintragen.
- Nach einem Verbindungsabbruch verbindet sich der Browser selbst neu; danach werden alle Queries neu
  geladen, weil verpasste Events nicht nachgeliefert werden.

## Design-System und App-Shell

Grundlage ist `docs/DESIGN.md`.

- **Design-Tokens** stehen als CSS-Variablen in `src/index.css`: neutrale Zinc-Grautöne, genau eine
  Akzentfarbe (`--brand`, ein gedecktes Blau; `--primary` und `--ring` leiten sich davon ab), Radius
  (`--radius`), Layout-Maße (`h-header`, `h-row`, `h-bottom-bar`) und die UI-Schriftgröße `text-ui`
  (13 px), der Hintergrund hinter Dialogen und Sheets (`bg-overlay`) und `scroll-fade` (blendet die
  Kante eines Scrollbereichs aus, solange `data-overflow` gesetzt ist). Optik über diese Tokens ändern,
  nicht über Ad-hoc-Klassen.
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
- Tastenhinweise (`KeyHint`) und die Shortcut-Übersicht werden nur angeboten, wenn eine Tastatur
  wahrscheinlich ist (`mediaQueries.keyboard`: ab 768 px und mit feinem Zeiger). Auf Handys und
  Touch-Tablets fehlen sie; die Shortcuts selbst funktionieren weiterhin.
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

`fetch` ist in allen Tests gemockt (`src/test/fetch.ts`): `/api/healthz` antwortet `ok`, alles
andere 404. Eigene Antworten mit `mockFetch((request) => json(...))`.

- Unit-Tests (Vitest): API-Client (CSRF, 401, Problem Details), Fehleranzeige, `useEvents` mit
  Mock-`EventSource`, Theme-Umschaltung inkl. `theme-init.js`, Shortcut-Parser/-Dispatcher,
  Registries, Command Palette, Navigation, mobile Variante.
- E2E (Playwright): axe-Check (WCAG 2.2 AA) aller Seiten in Hell/Dunkel bei 1440 px und 360 px, keine
  horizontale Überbreite, Tastaturbedienung, Theme vor dem App-Bundle, keine externen Requests.

