# Barrierefreiheit

Stand des Audits aus #123: Die ganze App wurde gegen **WCAG 2.2 AA** und die Tastatur-Vorgaben aus
`docs/DESIGN.md` geprüft. Dieser Bericht beschreibt, was geprüft wird, was behoben ist und welche
Einschränkungen bekannt sind. Er gilt für das Frontend; Mail-HTML Dritter ist ausgenommen (siehe
unten).

## Was geprüft wird (automatisch, in der CI)

| Bereich | Wie | Wo |
|---|---|---|
| axe (WCAG 2.2 A/AA, inkl. Zielgrößen 2.5.8) für **jede Route** | „mit Daten“ und „leer“ in Hell/Dunkel × 1440 px/390 px; „Fehler“ (API 500) und „Laden“ (Skeletons) in Hell/Desktop und Dunkel/Mobil; Login, Setup, Einladung, 403, 404 | `frontend/e2e/a11y.spec.ts`, Routen und Zustände in `e2e/app-states.ts` |
| axe für geöffnete Overlays | Command Palette, Shortcut-Übersicht, Dialoge (Konto löschen, …), Sheets (Nutzer einladen, Anbieter, …), Menüs | `e2e/a11y.spec.ts` |
| axe in Abläufen | Mail öffnen, Antwort-Stream, Entwürfe, Aufgaben, Digest, Export | jeweilige Feature-Specs, Helfer `e2e/a11y.ts` |
| Tastatur | Skip-Link, Tab-Reihenfolge der Inbox (Roving Tabindex), `j`/`k`/`e`/`⌘K`/`?`, keine Shortcuts in Textfeldern, `Enter`/`Leertaste` auf Buttons | `e2e/keyboard.spec.ts`, `src/lib/shortcuts.test.ts` |
| Fokus-Falle und Fokus-Rückgabe | Command Palette, Shortcut-Sheet, Dialoge | `e2e/keyboard.spec.ts` |
| Shortcut-Übersicht | vollständig je Seite, übersetzt (DE/EN) | `e2e/keyboard.spec.ts` |
| Landmarks, Überschriften, Seitentitel | genau ein `h1`, keine übersprungenen Ebenen, eigener `<title>` je Seite | `e2e/keyboard.spec.ts` |
| Live-Regionen | Sync-Status der Postfächer, Toasts, Antwort-Stream (`aria-live`, `aria-busy`) | `e2e/keyboard.spec.ts`, `e2e/search.spec.ts` |
| Kontrast der Design-Tokens | alle Text-/Flächenpaare ≥ 4,5:1, Fokusring, Akzent und Rahmen von Eingabefeldern ≥ 3:1, beide Themes | `src/design-tokens.test.ts` |
| Reflow und Zoom | keine horizontale Scrollleiste bei 320 CSS px (1.4.10) und bei 200 % Zoom (640 × 400), Navigation erreichbar | `e2e/reflow.spec.ts` |
| Textabstände (1.4.12) | größere Zeilen-, Buchstaben- und Wortabstände schneiden keinen Text ab | `e2e/reflow.spec.ts` |
| Reduzierte Bewegung | mit `prefers-reduced-motion: reduce` laufen keine Übergänge (globale Regel in `src/index.css`) | `e2e/reflow.spec.ts` |

Neue Seiten kommen in `routes` (`e2e/app-states.ts`), neue Dialoge und Sheets in `overlays`
(`e2e/a11y.spec.ts`). Neue Design-Tokens und Flächen ergänzt man in `src/design-tokens.test.ts`.

## Was behoben ist

**Tastatur und Fokus**
- Inbox: Jede Mailzeile war ein Tab-Stopp (bis zu 10.000, virtualisiert); per `Tab` kam man nicht
  aus der Liste heraus. Jetzt hat die Liste einen Roving Tabindex: `↑`/`↓`/`Pos1`/`Ende` und
  `j`/`k` bewegen den Fokus.
- `Enter`/`Leertaste` auf fokussierten Buttons wurden von Seiten-Shortcuts abgefangen (Inbox: Mail
  öffnen, Digest: Play/Pause). Der Dispatcher überlässt sie jetzt dem Bedienelement.
- Overlays, die per Shortcut, Command Palette oder Menüeintrag geöffnet werden, gaben den Fokus
  nicht zurück. Dialoge und Sheets geben ihn jetzt an das vorher fokussierte Element zurück.
- `e` (erledigt) fehlte; auf `/tasks` erledigt `e` die aktive Aufgabe.
- Das Audit-Log war auf dem Handy nicht per Tastatur scrollbar; es ist jetzt ein fokussierbarer
  Bereich.
- Modale Dropdown-Menüs versteckten die Seite mit `aria-hidden`, obwohl ihre Elemente fokussierbar
  blieben. Menüs sind jetzt nicht modal.

**Struktur**
- Jede Seite hat einen eigenen Seitentitel („Aufgaben – ollamail“). Betreffzeilen kommen nie in den
  Titel, weil sie sonst im Browser-Verlauf landen (`docs/PRIVACY.md`).
- Split-Ansichten hatten zwei `h1`; der Detailbereich nutzt jetzt `h2`.
- Der Sync-Status der Postfächer ist eine Live-Region.

**Kontrast**
- Rahmen von Eingabefeldern, Checkboxen, Radio-Buttons und Schaltern (`--input`): 1,4:1 → ≥ 3:1.
- Fokusring: Er war mit 50 % Deckkraft gezeichnet (2,0–2,6:1) und hat jetzt 80 % (≥ 3:1 auf allen
  Flächen).
- Fehlertext (`--destructive`, hell) auf getönten Flächen: 4,2:1 → ≥ 4,5:1.

## Bekannte Einschränkungen

- **Kein „Erledigt“ in der Inbox.** `e` ist nur auf `/tasks` belegt. Für Mails fehlt ein
  Backend-Endpunkt zum Archivieren/Erledigen; `PATCH /messages/{id}` kennt nur `seen`. Das ist ein
  eigenes Thema für ein neues Issue.
- **Mail-HTML** ist Inhalt Dritter. Es läuft in einer Sandbox ohne Skripte, und axe kann dort nicht
  prüfen. Kontrast und Struktur bestimmt der Absender. Die App rendert es hell, auch im dunklen
  Theme.
- **Gestapelte Toasts** blenden ältere Einträge absichtlich aus (sonner). axe prüft den
  Toast-Bereich deshalb in einer Spec (`task-export-gtasks.spec.ts`) nicht.
- **Nur Chromium.** Die E2E-Tests laufen in Chromium. Firefox und Safari sowie Screenreader (NVDA,
  VoiceOver, TalkBack) sind nicht automatisiert getestet; ein manueller Durchgang steht aus.
- **Zielgrößen:** Geprüft wird AA (2.5.8, 24 × 24 px oder ausreichender Abstand), nicht AAA
  (44 × 44 px). Kleine Checkboxen (16 px) bestehen über die Abstandsregel.
- **Sync-Status:** Während eines Erstimports kann die Live-Region häufig ansagen (ein Update je
  importiertem Ordner).
- **Digest:** In der Split-Ansicht stehen der Digest-Titel und „Transkript“ beide als `h2`
  nebeneinander, nicht verschachtelt.
- **Fehler- und Ladezustände** prüft axe in zwei der vier Theme-/Größen-Kombinationen, weil die
  Komponenten seitenübergreifend dieselben sind. Das hält den E2E-Job in seinem Zeitbudget.

## Selbst prüfen

```sh
cd frontend
pnpm test                                   # u. a. Kontrast der Tokens
pnpm e2e e2e/a11y.spec.ts e2e/keyboard.spec.ts e2e/reflow.spec.ts
```
