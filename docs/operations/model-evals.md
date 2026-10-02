# Modell-Evaluierung

Wie gut funktionieren Triage, Aufgaben-Erkennung, Tageszusammenfassung (Digest) und „Frag dein
Postfach“ (RAG) mit einem bestimmten lokalen Modell? Die Eval-Suite in `backend/app/evals/` misst
das reproduzierbar auf einem synthetischen Datensatz – mit denselben Prompts, derselben
Nachverarbeitung und demselben `LLMGateway` wie die Features. Sie ist die Grundlage für die
Standardmodelle der Hardware-Profile (`cpu`, `gpu-consumer`, `gpu-server`,
[ARCHITECTURE.md §3.2](../ARCHITECTURE.md#32-llm-provider)).

## 1. Was gemessen wird

| Stufe | Ablauf (Feature-Code) | Metriken |
|---|---|---|
| `triage` | Vorfilter (`app.triage.rules.prefilter`), dann `app.triage.classify.classify` mit den sieben Standardkategorien, ohne Absenderregeln und Few-Shot-Beispiele (neuer Nutzer) | Accuracy, Accuracy nur der Modell-Entscheidungen, Priorität, Konfusionsmatrix, Recall je Kategorie |
| `todos` | Prompt `todos_extract` über `app.todos.extraction.build_prompt`, Nachverarbeitung `plan_extraction` (Konfidenzfilter, deterministische Fristen); Mails der Kategorien aus `OLLAMAIL_TODOS_SKIP_CATEGORIES` gehen wie im Schritt `todos` nicht ans Modell | Precision, Recall, F1; Titel per Fuzzy-Match (≥ 0,6) oder Schlüsselwort, Fälligkeit exakt |
| `digest` | `app.digest.summarize.Summarizer` (Map, Condense, Reduce) je Tag und Sprache; Auswahl und Reihenfolge wie `app.digest.content` (Sammelkategorien nur gezählt, Spam weggelassen) | Anteil der wichtigen Mails, die der Text mit `[n]` referenziert; Anteil der genannten Fristen; Wörter, Dauer |
| `rag` | Mails speichern und mit `index_message` indexieren (Chunks, Volltext, Embeddings), dann `RagService.ask` je Frage: Query-Analyse, Hybrid-Suche, ggf. Reranking, Antwort-Prompt, Zitatfilter | Recall@1/3/5 und Recall über alle ans Modell gegebenen Quellen, MRR, zitierte Quelle, Antwort korrekt (Regeln), Fragen ohne Antwort korrekt abgelehnt, optional LLM-as-Judge |
| alle | Metriken des Gateways je Aufruf (ohne Inhalte) | Sekunden je Aufruf (Mittel, p95), erzeugte Tokens/s, verarbeitete Tokens/s (Prompt + Antwort) |

Bewertungsregeln im Detail:

- **Triage:** Mails, die der Vorfilter entscheidet (Header wie `List-Unsubscribe`, `Auto-Submitted`,
  Roboter-Absender), erreichen das Modell nicht – wie im Betrieb. „Accuracy (model only)“ zählt nur
  die Mails, die das Modell entschieden hat. `--no-prefilter` schickt alle Mails ans Modell.
- **Todos:** Eine erkannte Aufgabe passt zu einer erwarteten, wenn eines der erwarteten
  Schlüsselwörter in Titel oder Beschreibung vorkommt oder die Titel ähnlich genug sind
  (Zeichenvergleich, auch mit sortierten Wörtern). Jede Aufgabe zählt höchstens einmal. Die Frist
  muss exakt stimmen (beide ohne Frist zählt als richtig). Für die Auswahl, welche Mails ans Modell
  gehen, zählt die *erwartete* Kategorie, damit Triage-Fehler die Aufgaben-Zahlen nicht verfälschen.
- **Digest:** Der Text ist frei formuliert, deshalb nur grobe Regeln: Referenzen auf wichtige Mails
  (`action_required`, `important` oder Priorität 1) und ob ein Schlüsselwort der Frist
  (Wochentag, Monat, Zahl) im Text steht. Inhaltliche Qualität (richtige Aussagen, Stil) misst das
  nicht.
- **RAG:** Antworten gelten als korrekt, wenn sie aus jeder erwarteten Schlüsselwortgruppe eine
  Variante enthalten (z. B. `["14:30", "14.30"]` und `["raum b2"]`). Fragen ohne Antwort im
  Postfach sind korrekt behandelt, wenn die Antwort nichts zitiert (Status `no_evidence`) oder
  sagt, dass nichts gefunden wurde. Der optionale **LLM-Judge** (`--judge-model`) bewertet
  zusätzlich frei; sein Ergebnis steht getrennt im Bericht, zusammen mit dem Anteil der Fragen, bei
  denen er den Regeln widerspricht. Ein kleiner Judge irrt oft – die Zahl ist ein Hinweis, kein
  Maßstab.
- **Tokens/s:** Ollama meldet Token-Zahlen pro Aufruf; die Rate bezieht sich auf die gesamte
  Aufrufdauer (Prompt-Verarbeitung eingeschlossen). Auf CPU dominiert bei langen Prompts und kurzen
  Antworten die Prompt-Verarbeitung, deshalb gibt es zusätzlich „processed tok/s“. RAG-Antworten
  werden gestreamt; ihre Token-Zahl ist aus der Länge geschätzt (≈ 3 Zeichen je Token).

## 2. Datensatz

`backend/app/evals/data/`:

- `mails.json`: 200 Mails (100 DE, 100 EN) einer erfundenen Person („Robin Beispiel“,
  `robin@example.org`, Zeitzone Europe/Berlin) aus der Woche 5.–9. Oktober 2026, je Sprache
  Berufs- und Privatleben. Je Mail: erwartete Kategorie und Priorität, erwartete Aufgaben mit
  Titel, Schlüsselwörtern, Frist im Wortlaut (`due_phrase`) und als Datum. Kategorien je 50 Mails:
  important 7, action_required 10, waiting_for 6, info 8, newsletter 7, notification 7, spam 5
  (darunter Phishing und ein Prompt-Injection-Versuch). 60 erwartete Aufgaben, 41 mit Frist.
- `questions.json`: 60 Fragen (30 DE, 30 EN), davon 48 mit Antwort (Quell-Mail und
  Schlüsselwortgruppen) und 12 ohne Antwort im Postfach.

Alle Daten sind erfunden ([PRIVACY.md](../PRIVACY.md)): Namen ausgedacht, Adressen und Links
nur unter `example.com`/`example.org`. Die Tests (`tests/evals/test_dataset.py`) prüfen das, dazu,
dass jede Frist wörtlich in der Mail steht und genau das Datum ergibt, das
`app.todos.dates.parse_due_phrase` berechnet, und dass jede erwartete Antwort in ihrer Quell-Mail
steht. Neue Mails und Fragen im selben Format ergänzen; Betreffzeilen müssen eindeutig sein.

## 3. Ausführen

Voraussetzungen: ein Ollama- oder OpenAI-kompatibler Endpunkt mit den Modellen und – nur für die
RAG-Stufe – eine **eigene** PostgreSQL-Datenbank mit pgvector, migriert. Die RAG-Stufe schreibt
alles in eine Transaktion, die am Ende zurückgerollt wird; dabei wird die Vektorspalte an das
Embedding-Modell angepasst (sperrt die Tabelle). Deshalb nie die Produktionsdatenbank verwenden.

```bash
cd backend
# Datenbank für die RAG-Stufe (einmalig)
createdb ollamail_eval
OLLAMAIL_DATABASE_URL=postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail_eval \
  uv run alembic upgrade head

# Modelle
ollama pull qwen2.5:3b && ollama pull llama3.2:1b && ollama pull bge-m3

# Lauf: zwei Modelle im Vergleich, alle Stufen, Bericht nach ./eval-results
OLLAMAIL_EVAL_DATABASE_URL=postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail_eval \
uv run python -m app.evals \
  --base-url http://localhost:11434 \
  --model qwen2.5:3b --model llama3.2:1b \
  --embedding-model bge-m3 \
  --output eval-results
```

Optionen:

| Option | Bedeutung |
|---|---|
| `--model NAME` | Chat-Modell (mehrfach für einen Vergleich); bedient alle Chat-Tasks. Ohne: die Modelle aus der Umgebung |
| `--stage triage\|todos\|digest\|rag` | nur diese Stufen (mehrfach); Standard: alle |
| `--base-url`, `--provider ollama\|openai_compatible` | Endpunkt; sonst `OLLAMAIL_LLM_BASE_URL`, `OLLAMAIL_LLM_PROVIDER` (API-Key: `OLLAMAIL_LLM_API_KEY`) |
| `--embedding-model` | Embedding-Modell für den RAG-Index; sonst `OLLAMAIL_LLM_TASK_EMBEDDINGS_MODEL` bzw. Profil |
| `--judge-model` | RAG-Antworten zusätzlich von diesem Modell bewerten lassen |
| `--database-url` | Datenbank der RAG-Stufe; sonst `OLLAMAIL_EVAL_DATABASE_URL`. Fehlt sie, wird die RAG-Stufe übersprungen |
| `--language de\|en`, `--limit N` | Teilmenge: nur eine Sprache bzw. N gleichmäßig verteilte Mails (Fragen, deren Quelle fehlt, entfallen) |
| `--no-prefilter` | Triage ohne Vorfilter |
| `--output DIR` | `report.md` und `report.json` schreiben; sonst Markdown auf stdout |

Alle übrigen Einstellungen kommen wie im Betrieb aus der Umgebung (`OLLAMAIL_LLM_PROFILE` für
Kontextfenster und Reranker, `OLLAMAIL_LLM_STRUCTURED_OUTPUT_RETRIES`, `OLLAMAIL_RAG_*`,
`OLLAMAIL_DIGEST_*`, `OLLAMAIL_TODOS_MIN_CONFIDENCE`, …). Der Exit-Code ist 1, wenn ein Modell
komplett fehlgeschlagen ist.

**Bericht:** `report.json` enthält alle Zahlen, Rechner (CPU, Kerne), Endpunkt (ohne Zugangsdaten),
Prompt-Versionen und je Modell die Fehler als IDs des Datensatzes. `report.md` vergleicht die
Modelle in Tabellen. Prompts, Antworten und Mail-Texte stehen nie im Bericht.

**CI:** Der Lauf gehört nicht zu `ci-ok`. Die Unit-Tests (`tests/evals/`) laufen in der normalen
Suite gegen ein gemocktes Modell. Für echte Läufe gibt es den manuell startbaren Workflow
„Model evals“ (`.github/workflows/model-evals.yml`, Actions → Model evals → Run workflow): Ollama
auf der CPU des Runners, Standard 60 Mails; Bericht als Artefakt und in der Zusammenfassung.

## 4. Ergebnisse

<!-- results -->
