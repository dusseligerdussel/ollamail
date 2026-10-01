# Design-Richtlinien

Ziel: ein ruhiges, schnelles, professionelles Werkzeug. Vorbilder: Linear, Superhuman, Things.
**Es soll nicht nach „KI-Produkt“ aussehen.**

## Do

- Viel Weißraum, klare Typografie, wenige Farben. Neutrale Grautöne (z. B. zinc), **eine** Akzentfarbe.
- Systemschrift bzw. selbst gehostete Variable Font (z. B. Inter oder Geist). Keine Google-Fonts-CDNs.
- Dichte Listen wie in einem Mail-Client: eine Zeile pro Mail, Kategorie als dezentes Label.
- Tastatur zuerst: `j/k` navigieren, `e` erledigt, `⌘K` Command Palette, `?` Shortcut-Übersicht.
- Dark-, Light- und System-Theme, gleichwertig gestaltet.
- Lucide-Icons, 16 px, Strichstärke einheitlich.
- Schnelle, kurze Übergänge (≤ 150 ms), `prefers-reduced-motion` respektieren.
- Leere Zustände sachlich mit einer klaren nächsten Aktion.
- Skeletons statt Spinner bei Listen.
- Barrierefreiheit: WCAG 2.2 AA, Fokus sichtbar, alles per Tastatur bedienbar.

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

## Komponenten

- Basis ausschließlich shadcn/ui-Komponenten (in `frontend/src/components/ui`), Anpassungen über Design-Tokens
  (CSS-Variablen), nicht durch Ad-hoc-Klassen.
- Eigene, wiederverwendbare Komponenten in `frontend/src/components`.
- Texte über i18n-Keys, nie hartkodiert.
