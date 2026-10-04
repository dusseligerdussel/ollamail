# Design-Richtlinien

Ziel: ein ruhiges, schnelles, professionelles Werkzeug. Vorbilder: Linear, Superhuman, Things.
**Es soll nicht nach „KI-Produkt“ aussehen.**

## Do

- Viel Weißraum, klare Typografie, wenige Farben. Neutrale Grautöne (z. B. zinc), **eine** Akzentfarbe.
- Systemschrift bzw. selbst gehostete Variable Font (z. B. Inter oder Geist). Keine Google-Fonts-CDNs.
- Dichte Listen wie in einem Mail-Client: eine Zeile pro Mail, Kategorie als dezentes Label.
- Tastatur zuerst: `j/k` navigieren, `e` erledigt (archivieren), `v` verschieben, `#` Papierkorb,
  `s` markieren, `u` gelesen/ungelesen, `⌘K` Command Palette, `?` Shortcut-Übersicht. Aktionen
  wirken sofort und bieten „Rückgängig“ im Toast statt einer Rückfrage.
- Dark-, Light- und System-Theme, gleichwertig gestaltet.
- Lucide-Icons, 16 px, Strichstärke einheitlich.
- Schnelle, kurze Übergänge (≤ 150 ms), `prefers-reduced-motion` respektieren.
- Leere Zustände sachlich mit einer klaren nächsten Aktion. Fehler beim Laden ebenso:
  `InlineError` mit „Erneut versuchen“ (`onRetry`). Schickt der Server `Retry-After`, ist der
  Button bis dahin deaktiviert und zählt herunter (`RetryButton`).
- „Zu viele KI-Anfragen“ (`llm_busy`) ist kein Fehler, sondern eine Wartezeit: neutral (Info-Icon,
  kein Rot) und mit „Erneut versuchen“ für dieselbe Frage bzw. denselben Entwurf.
- Skeletons statt Spinner bei Listen; sie haben die Zeilenhöhe der echten Liste (Mail-Listen mobil
  zweizeilig), damit nichts springt.
- Barrierefreiheit: WCAG 2.2 AA, Fokus sichtbar, alles per Tastatur bedienbar. Prüfumfang, Funde und
  bekannte Einschränkungen: `docs/accessibility.md`.

## Don't

- Keine Farbverläufe, Glow-Effekte, Glassmorphism, Neon-Lila.
- Keine Sparkle-/Zauberstab-Icons, kein „✨ AI“, keine Emojis als UI-Elemente.
- Keine Chat-Bubbles mit Avatar-Robotern. Der RAG-Chat ist ein schlichtes Frage/Antwort-Panel mit Quellen.
- Keine Marketing-Sprache in der UI („magisch“, „smart“, „powered by AI“). Funktionen werden benannt, was sie tun:
  „Zusammenfassung“, „Aufgaben“, „Suche“.
- Keine Modals für Dinge, die in einen Seitenbereich (Sheet) oder inline passen.

## Layout

```
┌────────┬──────────────────────┬──────────────────────────────┐
│ Nav    │ Liste                │ Detail                       │
│ Inbox  │ (Triage-Gruppen)     │ Mail / Thread / Todo         │
│ Aufg.  │                      │                              │
│ Digest │                      │                              │
│ Suche  │                      │                              │
│ ─────  │                      │                              │
│ Admin  │                      │                              │
└────────┴──────────────────────┴──────────────────────────────┘
```

Mobil: Navigation als Bottom-Bar oder Sheet, Liste und Detail als gestapelte Ansichten.

Einstellungs- und Admin-Seiten (Formulare, Abschnitte mit Zeilen) nutzen eine gemeinsame Spalte
`max-w-2xl` (624 px Inhalt). Breiter sind nur Lesebereiche (Thread, Digest, Aufgaben, Entwürfe:
`max-w-3xl`) und Tabellen (volle Breite).

Die Hauptaktion einer Seite („Provider hinzufügen“, „Kategorie anlegen“ …) steht als Primary-Button
(`size="sm"`) rechts im Seitenkopf – auch wenn sie nur einen Abschnitt der Seite betrifft. In
Abschnittsköpfen stehen keine Buttons.

## Typografie

| Rolle | Größe | Klasse |
|---|---|---|
| Seitentitel (h1 im Seitenkopf) | 14 px, medium | `text-sm font-medium` |
| Fließtext, Listenzeilen, Buttons `sm` | 13 px | `text-ui` |
| Abschnittstitel, Meta, Labels über Feldern | 12 px | `text-xs` |

In dichten Ansichten sind Buttons `size="sm"` (13 px); damit bleibt der Seitentitel die größte
Schrift im Kopf. Eingabefelder und Auswahllisten haben mobil 16 px (sonst zoomt iOS beim Fokus),
ab `md` 14 px; `NativeSelect size="sm"` ab `md` 13 px. Größere Buttons (`default`, 14 px) nur in Dialogen und auf öffentlichen Seiten
(Anmeldung, Einrichtung).

## Komponenten

- Basis ausschließlich shadcn/ui-Komponenten (in `frontend/src/components/ui`), Anpassungen über Design-Tokens
  (CSS-Variablen), nicht durch Ad-hoc-Klassen.
- Eigene, wiederverwendbare Komponenten in `frontend/src/components`.
- Texte über i18n-Keys, nie hartkodiert.
- Segmented Controls (Auswahl aus wenigen Optionen, z. B. Theme, Wochentage): `ToggleGroup` mit
  `variant="segmented"`. Die gewählte Option ist angehoben und in der Akzentfarbe umrandet
  (≥ 3:1, WCAG 1.4.11) – nie nur durch einen leicht anderen Grauton erkennbar.
- Ein/Aus-Einstellungen sind ein `Switch` in einer Zeile mit Label und Beschreibung, kein
  Segmented Control. Ist ein Schalter deaktiviert, sagt die Beschreibung, warum.
- Aktionen hinter einer erneuten Bestätigung (`useReauth`) übergeben ihren Namen
  (`useReauth({ action })`); speichert die Aktion ein Formular, zusätzlich `unsavedChanges`, damit
  der Dialog vor „Abmelden und neu anmelden“ warnt.
- Globale Hinweisleisten (`components/system-notices.tsx`) stehen auf dem Desktop untereinander;
  auf Mobil sind sie zu einer einzeiligen, aufklappbaren Leiste („2 Hinweise“) zusammengefasst.
  Rein informative Hinweise (Cloud-KI) lassen sich pro Sitzung ausblenden, Warnungen nicht.
