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

## E2E-Tests (Playwright)

Die Specs in `e2e/` laufen in zwei Arten:

- **Gemockt** (Standard): Die API wird im Browser beantwortet (`e2e/mock-*.ts`), kein Backend nötig.
- **Gegen den echten Stack** (`E2E_API=1`, zusätzlich `E2E_IMAP=1`): Backend, Worker, PostgreSQL und
  der Dovecot-Testserver der Backend-Tests, ohne LLM. Abgedeckt sind Setup (Erst-Admin) und Login
  (`auth.spec.ts`), IMAP-Postfach anlegen und Mails in der Inbox (`mailbox.spec.ts`), Aufgabe abhaken
  und Volltextsuche (`fullstack.spec.ts`). Gemeinsame Helfer: `e2e/stack.ts`. Ohne die Variablen
  überspringen sich diese Specs selbst.

Projekte in `playwright.config.ts`: `setup` (`auth.spec.ts`, braucht eine leere Datenbank) läuft
zuerst, danach parallel `chromium` (alles andere), zum Schluss allein `perf` (Tests mit Tag `@perf`,
die Main-Thread-Arbeit messen; Budgets siehe „Postfächer und Inbox“, E2E). Es gibt keine Retries: Ein Test, der erst im zweiten Versuch grün wird,
deckt einen Fehler zu. Gewartet wird auf Zustände (`expect`, `toPass`), nie mit festen Pausen.
`E2E_PREVIEW=1` testet den Produktions-Build (`pnpm build` vorher) statt des Dev-Servers.

**Barrierefreiheit (axe):** `e2e/a11y.spec.ts` prüft jede Route mit axe (WCAG 2.2 A/AA,
Helfer `expectNoA11yViolations` in `e2e/a11y.ts`) in den Zuständen „mit Daten“ und „leer“ in
Hell/Dunkel × Desktop (1440 px)/Handy (390 px), „Fehler“ (alle Anfragen 500) und „Laden“ (keine
Antwort) in Hell/Desktop und Dunkel/Handy, dazu Anmelde-, Setup-, Einladungs- und 403-Seite sowie
geöffnete Dialoge, Sheets, Menüs und die Command Palette. Neue Routen gehören in die Liste
`routes`, neue Dialoge/Sheets in `overlays`. Die Admin-API mockt `e2e/mock-admin.ts`
(`{ empty: true }` für leere Zustände). Feature-Specs nutzen denselben Helfer für ihre Abläufe.

Lokal gegen den echten Stack (wie der CI-Job „E2E“ in `.github/workflows/ci.yml`):

```sh
docker run -d -p 5432:5432 -e POSTGRES_USER=ollamail -e POSTGRES_PASSWORD=ollamail \
  -e POSTGRES_DB=ollamail_e2e pgvector/pgvector:pg16
docker run -d -p 31993:31993 -e USER_PASSWORD=ollamail-test dovecot/dovecot:2.4.5

# in backend/, in zwei Terminals mit denselben Variablen:
export OLLAMAIL_DATABASE_URL=postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail_e2e
export OLLAMAIL_SECRET_KEY="$(openssl rand -base64 32)" OLLAMAIL_SETUP_TOKEN=e2e-setup-token
export OLLAMAIL_DATA_DIR=/tmp/ollamail-e2e OLLAMAIL_AUTH_COOKIE_SECURE=false
export OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true OLLAMAIL_LLM_BASE_URL=http://127.0.0.1:9
uv run alembic upgrade head && uv run uvicorn app.main:app --port 8000
uv run python -m app.worker

# in frontend/:
E2E_API=1 E2E_IMAP=1 E2E_SETUP_TOKEN=e2e-setup-token pnpm e2e
```

Für einen zweiten Lauf die Datenbank leeren (`auth.spec.ts` prüft, dass sie leer ist). Bei Fehlern
lädt die CI den Playwright-Report (mit Traces) und die Logs von API und Worker als Artifact hoch.

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
- **Deine Daten** (`/settings`, `src/components/account/`): Datenexport anfordern (Status live über
  das Event `privacy.export`, Download-Link bis zum Ablauf) und Konto löschen (Dialog, Bestätigung
  durch Eingabe der eigenen E-Mail-Adresse; danach Login-Seite). API: `src/api/privacy.ts`.
- **Admin → Nutzer → „Nutzer löschen …“** (`/admin/users`, `src/components/admin/delete-user-dialog.tsx`):
  `DELETE /api/admin/privacy/users/{id}`. Der Dialog zählt auf, was gelöscht wird (eigene
  Postfächer, Mails, Aufgaben, Digests, …; Team-Postfächer bleiben), und verlangt die E-Mail-Adresse
  des Nutzers. `last-admin` und `admin-lockout` (409) erscheinen als eigener Hinweis im Dialog.
  Danach Toast mit der Zahl gelöschter Postfächer; beim eigenen Konto Warnung und danach Login-Seite.
- **Admin → Aufbewahrung** (`/admin/retention`): Fristen je Datenkategorie mit Standardwert aus der
  Umgebung, „Standard verwenden“, Hinweis bei Mail-Frist unter dem Erstimport, letzter Lauf.

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
  Adressfelds; der Vorschlag füllt die Serverfelder nur, solange der Nutzer keines davon geändert
  hat und gerade in keinem steht, auch wenn die Antwort spät kommt) und Verbindungstest; Fehler kommen als `error_code` und werden über
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
- **Scroll-Performance** (#111, CPU-only ist ein Kernziel): Zeilen (`MessageRow`) sind memoisiert und
  bekommen nur stabile Props; Übersetzung, Zeitzone und Datumsformat kommen einmal über einen Kontext
  der Liste, nicht per Hook in jeder Zeile. `getItemKey`/`estimateSize` des Virtualizers sind stabil
  (eine neue Funktion lässt ihn die Positionen aller Zeilen neu berechnen). `Intl.DateTimeFormat`
  wird je Sprache, Zeitzone und Stil nur einmal erzeugt (`src/lib/mail-format.ts`). Triage-Labels
  teilen Kategorien und Übersetzung über `HideTriageLabels`, je Zeile bleibt nur die Triage-Query.
- **E2E:** `e2e/inbox.spec.ts` (gemockt, `e2e/mock-mail.ts`: 10.000 Mails scrollen, Tastatur,
  blockierte Bilder, Mobil, axe). Der Scroll-Test (`@perf`, mit Triage-Labels) misst
  Main-Thread-Arbeit statt Frame-Zeiten, die ohne GPU von der Software-Rasterisierung abhängen:
  Script-Zeit je Scroll-Schritt (Chromium-Metrik `ScriptDuration`) und Total Blocking Time der
  Long Tasks. Budgets in `scrollBudget`, getrennt für Produktions-Build und Dev-Server (React-Dev-Build,
  etwa dreifache Arbeit); die Messwerte stehen als Annotation `scroll` im Testergebnis. `e2e/mailbox.spec.ts` gegen echte API, Worker und den
  IMAP-Testserver der Backend-Tests:

  ```sh
  # backend/: OLLAMAIL_SETUP_TOKEN=e2e OLLAMAIL_AUTH_COOKIE_SECURE=false \
  #   OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true … uv run uvicorn app.main:app  (+ python -m app.worker)
  E2E_API=1 E2E_IMAP=1 E2E_SETUP_TOKEN=e2e pnpm e2e e2e/mailbox.spec.ts
  ```

### Suche und Antworten (#26)

API in `src/api/search.ts`, Komponenten in `src/components/search/`, Seite `/search`.

- **Eine Oberfläche:** Stichwörter → Trefferliste (`POST /api/search`, ein Treffer je Mail,
  Suchbegriffe markiert). Fragen (endet auf „?“ oder beginnt mit einem Fragewort, `isQuestion()`
  in `src/lib/search-text.ts`; erzwingen mit `mod+Enter` bzw. „Als Frage beantworten“) →
  gestreamte Antwort über `POST /api/rag/ask`, darunter die Treffer als Kontext. Eine Frage, während
  ein Gespräch offen ist, ist eine Nachfrage im selben Gespräch.
- **Stream:** POST-SSE, gelesen mit `fetch` (`askQuestion()`, Parser `src/lib/sse.ts`);
  „Abbrechen“ bzw. `Esc` bricht den Request ab (der Server speichert dann nichts).
- **Quellen:** Zitatmarker `[n]` werden zu Schaltflächen, darunter die zitierten Quellen
  nummeriert. Ein Klick öffnet die Mail rechts (`?message=`) und markiert die Fundstelle
  (`ThreadView`-Prop `focus`; in HTML-Mails per CSS Custom Highlight API, ohne das Mail-DOM zu
  ändern; `src/lib/find-passage.ts`). Bei Treffern aus Anhängen wird der Anhang hervorgehoben.
- **Filter-Chips:** Zeitraum, Absender (inline editierbar), Postfach, Kategorie; gelten für
  Treffer und Fragen.
- **Datenschutz:** In der URL stehen nur IDs (`message`, `conversation`), nie die Suchanfrage.
- **Verlauf:** gespeicherte Gespräche (`/rag/conversations`), einzeln oder alle löschbar.
- **Tastatur:** `/` öffnet die Suche bzw. setzt den Cursor ins Feld, `Enter` sucht,
  `mod+Enter` fragt, `j`/`k` + `o`/`Enter` öffnen Treffer, `Esc` bricht ab bzw. schließt die Mail.
- **E2E:** `e2e/search.spec.ts` mit `e2e/mock-search.ts`; der Antwort-Stream wird im Browser
  erzeugt (umhülltes `fetch`), damit er wirklich stückweise ankommt.

### Aufgaben (#23)

API und Query-Keys in `src/api/todos.ts`, Komponenten in `src/components/tasks/`, Datumslogik in
`src/lib/task-dates.ts`. Backend-Vertrag: #22 (`GET/POST/PATCH /todos`, Filter `message_id`).

- **Seite `/tasks`:** Gruppen Überfällig / Heute / Demnächst / Ohne Datum / Erledigt. „Heute“
  bestimmt die Zeitzone im Profil (`useCurrentUser().timezone`), nicht die des Browsers.
  Offene Aufgaben werden vollständig geladen (bis 500), erledigte nur die letzten 50. Verworfene
  erscheinen nicht; „Verwerfen“ bietet im Toast „Rückgängig“ an.
- **Bearbeiten:** Klick (oder Enter) auf den Titel bearbeitet ihn inline (Enter/Verlassen speichert,
  Esc bricht ab). Das Datum öffnet ein Popover mit Heute / Morgen / Nächste Woche / Kein Datum und
  einem Datumsfeld. Das Feld „Neue Aufgabe“ legt per Enter an und bleibt für die nächste offen.
- **Optimistisch:** `useTodoMutations()` ändert alle gecachten Listen sofort (Seite und Mail),
  verschiebt Aufgaben zwischen offen/erledigt und rollt bei Fehlern zurück; danach wird neu geladen.
- **Tasten** (nur auf `/tasks`): `j`/`k` (setzt auch den Fokus), `x` erledigt/wieder offen,
  `d` Datum, `n` neue Aufgabe, `o` zur Mail, Enter Titel bearbeiten. Alle Aktionen auch in der
  Command Palette.
- **Mail-Detail:** `MessageTasksSlot` zeigt „Aufgaben aus dieser Mail“ (Query-Key
  `["message", "todos", id]`, damit `message.processed` neue Aufgaben nachlädt) und legt verknüpfte
  Aufgaben an. Der Slot registriert keine Einzeltasten, damit die Inbox-Tasten (#16, #21) frei bleiben.
- **Zur Mail:** `/inbox?message=<message_id>`.
- **E2E:** `e2e/tasks.spec.ts` (gemockt, `e2e/mock-todos.ts`): abhaken, Datum ändern, zur Mail
  springen, Tastatur, leerer Zustand, Mobil, axe in Hell/Dunkel.

### Triage (#21)

API und Query-Keys in `src/api/triage.ts`, Komponenten in `src/components/triage/`.

- **Label in der Liste** (`TriageLabel`, Slot `TriageLabelSlot compact`): dezentes Label mit dem
  Kategorienamen, hohe Priorität etwas kräftiger. Die Triage der sichtbaren Zeilen lädt
  `createTriageLoader` gebündelt (alle Anfragen desselben Ticks in einem
  `GET /triage/messages?ids=…`, höchstens 200 IDs je Anfrage).
- **Begründung im Detail** (`TriageReason`): eine Zeile über dem Thread, z. B. „Eingeordnet als
  Handlungsbedarf. <Begründung>“, bei Regeln „…, weil die Nachricht einen Abmeldelink enthält“.
  Rechts daneben öffnet „Kategorie ändern“ ein Menü zum Korrigieren.
  Können die Kategorien nicht geladen werden, steht dort ein Hinweis mit „Erneut versuchen“;
  die Mail bleibt lesbar. Bedingt gemountete Kinder bekommen die Kategorien als Prop statt eines
  eigenen `useCategories()` (jeder Mount auf eine fehlgeschlagene Query startet eine neue Anfrage, #86).
- **Korrektur:** Klick (Menü), Command Palette („Einordnen als …“) oder `c` → Kategorieauswahl, dort
  wählen die Ziffern `1`–`9` direkt (zwei Tastendrücke). Die Priorität bleibt erhalten.
- **Inbox nach Kategorie:** Auswahl „Ansicht“ im Seitenkopf bzw. Suchparameter `category`
  (`all` = gruppiert mit Überschriften, eine Kategorie-ID oder `none`). Quelle ist
  `GET /triage/inbox/messages` (sortiert nach Kategorie, Priorität, Datum); die Überschriften fügt
  `MessageList` über `groupHeader` ein. Für andere Ordner als den Posteingang gibt es die Ansicht nicht.
- **Live-Updates:** Das Event `message.triaged` invalidiert `["message", "triage"]` (Labels und die
  Inbox nach Kategorie), nicht die Threads.
- **Einstellungen → Kategorien** (`/settings/categories`): sortieren, ein-/ausblenden (mindestens eine
  bleibt sichtbar), eigene anlegen, Beschreibung bearbeiten, löschen. **Verwaltung → Kategorien der
  Organisation** (`/admin/categories`): Org-Defaults anlegen, bearbeiten, sortieren, löschen. Wird
  eine Standardkategorie umbenannt, übersetzt das UI sie nicht mehr.

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

### Antwortentwürfe (#93)

API und Query-Keys in `src/api/drafts.ts`, Komponenten in `src/components/drafts/`, Seite
`/drafts`. Backend-Vertrag: #92 (`docs/ARCHITECTURE.md`, Abschnitt 4.6).

- **Im Thread** (`ReplySlot` in `src/components/mail/slots.tsx`, unter den Mails): „Antworten“ /
  „Allen antworten“ legen einen Entwurf an (`POST /drafts`, Empfänger und Betreff bestimmt das
  Backend) und öffnen den Editor. Ein offener Entwurf der Mail erscheint beim Öffnen des Threads
  sofort wieder. Nur für Postfächer mit Recht `act`; geteilte Postfächer zeigen nichts.
- **Editor:** reiner Text. Änderungen werden 800 ms nach der letzten Eingabe gespeichert
  (`PATCH /drafts/{id}`), der Status steht in der Fußzeile. Ein Entwurf ohne Text wird beim
  Verlassen des Threads gelöscht. „Verwerfen“ ruft `POST /drafts/{id}/discard`.
- **Entwurf vorschlagen:** optionale Kurzanweisung, dann `POST /drafts/generate` mit `draft_id`
  (POST-SSE wie die Suche, `generateDraft()`); der Text läuft in den Editor, der währenddessen
  schreibgeschützt ist. `Esc`/„Abbrechen“ bricht ab, der vorherige Text kommt zurück (der Server
  speichert dann nichts). Fehler (`llm_unavailable` …) stehen im Editor.
- **Senden** nur über „Senden“ bzw. `⌘Enter`. Ist der Text noch genau der Vorschlag, verlangt der
  Editor eine zweite Bestätigung („Trotzdem senden“). Der Vorschlag wird dafür nur im Speicher der
  Seite gehalten (`src/lib/reply-draft.ts`), nie in `localStorage`. Vor dem Senden wird gespeichert.
  Fehler erscheinen im Editor, übersetzt nach `error_code` (`drafts.sendErrors.*`); der Entwurf
  bleibt offen.
- **Tasten:** `r` antworten, `a` allen antworten (beide im Thread; in Textfeldern nicht aktiv, `g a`
  bleibt die Admin-Navigation), `⌘Enter` senden, `Esc` bricht einen laufenden Vorschlag ab bzw.
  schließt die Anweisung. Antworten, Vorschlagen, Senden und Verwerfen auch in der Command Palette.
- **Übersicht** (`/drafts`, Navigation „Entwürfe“, `g r`): offene Entwürfe, zuletzt geänderte
  zuerst; Klick bzw. `j`/`k` + `o` öffnet die Mail mit dem Entwurf (`/inbox?message=`).
- **Query-Keys** unter `["drafts", …]`, nicht `["message", …]`: Mail-Events sollen einen Entwurf
  nicht während der Bearbeitung neu laden.
- **E2E:** `e2e/drafts.spec.ts` mit `e2e/mock-drafts.ts` (gemockter Stream im Browser wie bei der
  Suche): vorschlagen, bearbeiten, senden, Bestätigung bei unverändertem Vorschlag, Abbrechen,
  Sendefehler, Übersicht, axe in Hell/Dunkel, Mobil.

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
angemeldetem Admin (`/api/healthz`, Setup-Status, `auth/me`, Provider, Sitzungen, Datenschutz-Optionen
und leere Exportliste), alles andere 404.
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

