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
  hooks/                       useCurrentUser, useEvents, useListNavigation, useMediaQuery
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

### Anmeldung und Route-Guards

Backend-Vertrag: #11 (`docs/ARCHITECTURE.md`, Abschnitt Authentifizierung). Queries und Aufrufe
stehen in `src/api/auth.ts`.

- **Guard** (`beforeLoad` in `routes/__root.tsx`): lädt `GET /api/setup/status` und
  `GET /api/auth/me` (beide im Query-Cache). Nicht eingerichtet → `/setup`. Ohne Session →
  `/login?redirect=<Seite>`. Angemeldet auf `/login` oder `/setup` → weiter zum Ziel bzw. Posteingang.
  **Jede Route ist geschützt**, auch neue; öffentlich sind nur Routen mit `staticData: { public: true }`
  (ohne App-Shell, `PublicLayout`), die der Guard ausdrücklich behandelt.
- **`useCurrentUser()`** liefert den angemeldeten Nutzer (`UserRead` + `isAdmin`) aus dem Cache;
  nur in geschützten Routen verwenden. Änderungen über `useUpdateProfile()` (`PATCH /api/auth/me`)
  aktualisieren den Cache, `useChangeLanguage()` speichert die Sprache zusätzlich im Profil.
- **Admin-Seiten** prüfen `isAdmin` und zeigen sonst `<Forbidden />` (403). Die Rechte setzt die API
  durch (`require_admin`); das Frontend entscheidet nur, was es anzeigt.
- **Abmelden** (`useLogout()`): `POST /api/auth/logout`, danach lädt die Login-Seite neu, damit keine
  Daten im Speicher bleiben (Query-Cache, Router). Eine abgelaufene Session fällt beim nächsten
  API-Aufruf als 401 auf (Weiterleitung, siehe oben).
- **`?redirect=`** wird über `safeRedirect()` geprüft: nur Pfade dieses Origins, nie `/login`
  oder `/setup` (kein Open Redirect).
- **Login-Seite:** Die lokale Anmeldung erscheint bei `local_login`. Externe Provider kommen aus
  `GET /api/auth/providers`: Für `kind: "redirect"` (OIDC, GitHub) entsteht je ein Button, der per
  ganzer Seitennavigation `GET /api/auth/providers/{name}/login?redirect=<Pfad>` aufruft. Diesen
  Start-Endpunkt legen #30/#31 an. Passwort-Provider (LDAP, #32) bekommen ihren Platz im Formular,
  sobald #32 den Login-Vertrag festlegt.
- **Konto** (`/settings`): Name, E-Mail, Rolle, Zeitzone (IANA, im Profil gespeichert), Abmelden;
  Darstellung (Theme lokal, Sprache im Profil); aktive Sitzungen mit Abmelden einzelner bzw. aller
  anderen Geräte.

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

### Postfächer und Inbox (#16)

API und Query-Keys in `src/api/mail.ts`, Komponenten in `src/components/mail/`.

- **Postfächer** (`/settings/mailboxes`, `/settings/mailboxes/new`): Provider-Auswahl aus
  `GET /api/mailboxes/providers` – OAuth-Provider (Google, Microsoft 365) erscheinen nur, wenn das
  Backend sie als konfiguriert meldet. IMAP-Formular mit Autodiscovery (beim Verlassen des
  Adressfelds) und Verbindungstest; Fehler kommen als `error_code` und werden über
  `mailboxes.errors.<code>` übersetzt. OAuth: `POST <oauth_start_path>` mit `return_to`, danach
  ganze Seitennavigation zum Anbieter; das Ergebnis (`?graph=…` bzw. Gmails `/?mailbox_connected=…`)
  zeigt die Postfachliste einmal als Toast. Sync-Status live über die Events `mailbox.sync` und
  `mailbox.changed`, Ordnerauswahl im Sheet, Entfernen mit Bestätigung.
- **Inbox** (`/inbox`): Filter und geöffnete Mail stehen in der URL (`mailbox`, `folder`,
  `unread`, `message`). Die Liste ist mit TanStack Virtual virtualisiert (Zeilenhöhe `h-row`, auf
  Handys zweizeilig) und lädt Seiten nach, solange gescrollt wird; `total` sorgt für die richtige
  Scrollhöhe. Öffnen markiert als gelesen (einmal je Öffnen), `u` schaltet um. Tasten: `j`/`k`,
  `Enter`/`o`, `Esc`, `u`; Aktionen auch in der Command Palette.
- **Mail-HTML** zeigt `MailBodyFrame`: `iframe` mit `srcdoc`, `sandbox` **ohne** `allow-scripts`
  (`allow-same-origin` nur, damit die Höhe gemessen und `cid:`-Bilder mit Cookie geladen werden
  können), eigene CSP (`default-src 'none'`, Bilder nur `'self'`/`data:`), kein Referrer, Links in
  neuem Tab. Externe Bilder lädt erst „Bilder laden“ (`GET /messages/{id}/body?external_images=true`).
  Mail-HTML ist für hellen Hintergrund geschrieben und bleibt deshalb hell (im dunklen Theme als
  „Papier“).
- **Slots** für Triage-Label (#21) und „Aufgaben aus dieser Mail“ (#23): `src/components/mail/slots.tsx`.
- **E2E:** `e2e/inbox.spec.ts` (gemockt, `e2e/mock-mail.ts`: 10.000 Mails scrollen, Tastatur,
  blockierte Bilder, Mobil, axe). `e2e/mailbox.spec.ts` gegen echte API, Worker und den
  IMAP-Testserver der Backend-Tests:

  ```sh
  # backend/: OLLAMAIL_SETUP_TOKEN=e2e OLLAMAIL_AUTH_COOKIE_SECURE=false \
  #   OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true … uv run uvicorn app.main:app  (+ python -m app.worker)
  E2E_API=1 E2E_IMAP=1 E2E_SETUP_TOKEN=e2e pnpm e2e e2e/mailbox.spec.ts
  ```

### Digest (#29)

API und Query-Keys in `src/api/digest.ts`, Komponenten in `src/components/digest/`.

- **Seite** (`/digest`): links „Aktuell“ (neuester Digest) und „Archiv“, rechts der gewählte
  Digest (`?digest=<id>`, ohne Parameter auf breiten Bildschirmen der neueste). Laufende Digests
  zeigen ihren Status und werden per Event `digest.changed` aktualisiert (Fallback: Polling alle
  5 s, solange einer läuft). „Jetzt erzeugen“ in der Kopfzeile, im leeren Zustand und in der
  Command Palette.
- **Player** (`DigestPlayerProvider`): ein `<audio>`-Element für die ganze Seite, damit die
  Wiedergabe beim Wechsel ins Archiv (gestapeltes Mobil-Layout) weiterläuft; dann erscheint unter
  der Liste ein Mini-Player. Opus, wenn der Browser es abspielt, sonst MP3
  (`/api/digests/{id}/audio.{fmt}`, Range-Requests, nie vom Service Worker gecacht).
  Play/Pause, ±15 s, Position, Geschwindigkeit 1–2× (lokal gespeichert). Tasten: `Leertaste`,
  `←`/`→`, `<`/`>`. **Media Session API:** Titel auf Sperrbildschirm/Benachrichtigung,
  Play/Pause/Vor/Zurück/Springen über Kopfhörer und Sperrbildschirm, Position per
  `setPositionState`.
- **Transkript:** `parseScript()` (`src/lib/digest-script.ts`) zerlegt das Skript in Überschriften
  und Absätze; nur dieser Ausschnitt von Markdown wird interpretiert, nichts wird als HTML
  gerendert. `[n]` wird zum Link auf die Mail (`/inbox?message=<id>`).
- **Einstellungen** (`/digest/settings`): Änderungen werden sofort gespeichert
  (`PATCH /api/digests/settings`, nur das geänderte Feld). Stimmen aus `GET /api/digests/voices`,
  gefiltert nach der Sprache des Digests; ein Sprachwechsel setzt die Stimme auf den Standard
  zurück.
- **Podcast-Feed:** Die URL gibt es nur einmal, direkt nach `POST /api/digests/feed` (der Server
  speichert nur einen Hash). Sie bleibt nur im Zustand der Komponente (kein Query-Cache, kein
  `localStorage`) und ist mit Kopieren und QR-Code sichtbar, bis die Seite verlassen wird. Der
  QR-Code entsteht lokal mit [`uqr`](https://github.com/unjs/uqr) (MIT, ohne Abhängigkeiten) als
  SVG. Neu erzeugen und Abschalten verlangen eine Bestätigung inline; ein Hinweis erklärt, dass
  die URL ohne Anmeldung Zugriff gibt (`docs/PRIVACY.md`).
- **E2E:** `e2e/digest.spec.ts` (gemockt, `e2e/mock-digest.ts`).

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
- **Admin-Navigation** hängt an `useCurrentUser().isAdmin` (siehe „Anmeldung und Route-Guards“).

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

`fetch` ist in allen Tests gemockt (`src/test/fetch.ts`): eine eingerichtete Instanz mit
angemeldetem Admin (`/api/healthz`, Setup-Status, `auth/me`, Provider, Sitzungen), alles andere 404.
Andere Zustände mit `mockFetch(backend({ initialized: false, user: null }))`, eigene Antworten mit
`mockFetch((request) => json(...))`.

- Unit-Tests (Vitest): API-Client (CSRF, 401, Problem Details), Fehleranzeige, `useEvents` mit
  Mock-`EventSource`, Theme-Umschaltung inkl. `theme-init.js`, Shortcut-Parser/-Dispatcher,
  Registries, Command Palette, Navigation, mobile Variante.
- E2E (Playwright): axe-Check (WCAG 2.2 AA) aller Seiten (inkl. Login, Setup, 403) in Hell/Dunkel bei
  1440 px und 360 px, keine horizontale Überbreite, Tastaturbedienung, Theme vor dem App-Bundle, keine
  externen Requests. `e2e/shell.spec.ts` braucht kein Backend; die API wird im Browser gemockt
  (`e2e/mock-api.ts`).
- E2E gegen die echte API (`e2e/auth.spec.ts`): frische Instanz → Setup → Admin angemeldet → Logout
  → Login, Nicht-Admin ohne Admin-Navigation und mit 403-Seite. Braucht Backend und **leere**
  Datenbank hinter dem Dev-Proxy, sonst wird der Test übersprungen:

  ```sh
  # backend/: OLLAMAIL_SETUP_TOKEN=e2e OLLAMAIL_AUTH_COOKIE_SECURE=false … uv run uvicorn app.main:app
  E2E_API=1 E2E_SETUP_TOKEN=e2e pnpm e2e e2e/auth.spec.ts
  ```

