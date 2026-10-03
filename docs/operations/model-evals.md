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
- **Timeouts:** Manche Modelle erzeugen bei einzelnen Prompts endlos Text (z. B. JSON, das nie
  geschlossen wird, #132). Seit #133 begrenzt das Gateway Antwortlänge und Dauer jedes Aufrufs
  (`OLLAMAIL_LLM_MAX_OUTPUT_TOKENS`, `OLLAMAIL_LLM_CALL_TIMEOUT`, je Task überschreibbar); `--timeout`
  setzt die Frist für alle Chat-Tasks. Ein Aufruf über der Frist endet mit `LLMTimeoutError`,
  zählt in seiner Stufe als Fehler (falsche bzw. fehlende Antwort) und erscheint im Bericht als
  eigene Kennzahl: Timeouts je Task und Timeout-Quote über alle Chat-Aufrufe.
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
| `--timeout SEK` | Frist je Modellaufruf für alle Chat-Tasks (setzt `OLLAMAIL_LLM_CALL_TIMEOUT` und die Task-Werte); ohne: wie konfiguriert, sonst die Profil-Standardwerte (#133) |
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

### 4.1 Messung vom 2. Oktober 2026 (nur CPU)

**Umgebung:** Agent-Container, 4 vCPUs (Intel Xeon @ 2,80 GHz), 16 GB RAM, **keine GPU**.
Ollama 0.35.0 in Docker, Profil `cpu` (Kontext 8192 Tokens), Embeddings
`granite-embedding-multilingual:278m`. Chat-Modelle: `qwen2.5:3b` (Q4_K_M, Standardmodell des
Profils `cpu`) und `llama3.2:1b` (Q4_0). `ollama.com` war aus der Umgebung nicht erreichbar; die
Modelle stammen als GGUF aus den `ai/*`-Repositories von Docker Hub und wurden mit
`ollama create` importiert. `bge-m3` (Standard-Embedding des Profils) gab es dort nicht.
Frist je Aufruf: 120 s. Vollständige Berichte (Markdown und JSON):
[`model-evals/2026-10-02-cpu/`](model-evals/2026-10-02-cpu/).

| Lauf | Umfang | Code-Stand |
|---|---|---|
| `qwen2.5:3b`, Triage, Todos, Digest | **voller Datensatz** (200 Mails) | vor #133 (ohne Antwortlimit) |
| `qwen2.5:3b`, RAG | **Stichprobe**: 24 der 60 Fragen, alle 200 Mails indexiert | nach #133 |
| `llama3.2:1b`, alle Stufen | **Stichprobe**: 30 Mails, 12 Fragen (Index nur dieser 30 Mails) | nach #133 |

Der volle Lauf für `qwen2.5:3b` brauchte für Triage, Todos und Digest 2 h 25 min. Um das Zeitbudget
einzuhalten, wurde er vor der RAG-Stufe beendet (Endpunkt bewusst gestoppt, die RAG-Werte dieses
Laufs sind verworfen); RAG und `llama3.2:1b` wurden danach als Stichproben gemessen. Die
Stichproben sind klein, ihre Werte schwanken entsprechend; die RAG-Werte von `llama3.2:1b` sind
wegen des kleineren Index nicht mit denen von `qwen2.5:3b` vergleichbar.

| Metrik | `qwen2.5:3b` | `llama3.2:1b` (Stichprobe) |
|---|---|---|
| Triage: Accuracy (davon nur Modell) | **61,0 %** (56,4 %), 200 Mails | **13,3 %** (3,7 %), 30 Mails |
| Triage: Priorität richtig | 58,0 % | 23,3 % |
| Vorfilter (ohne Modell) | 21 Mails, alle richtig | 3 Mails, alle richtig |
| Todos: Precision / Recall / F1 | 24,4 % / 63,3 % / 35,2 % (60 erwartet, 156 erkannt) | 29,4 % / 76,9 % / 42,6 % (13 erwartet, 34 erkannt) |
| Todos: Frist richtig (bei erkannten) | 52,6 % von 38 | 50,0 % von 10 |
| **Todos: Timeouts** | **38 von 124 Aufrufen (30,6 %)** | 0 von 13 |
| Digest: wichtige Mails referenziert | 27,9 % (10 Digests) | 58,3 % (8 Digests) |
| Digest: Fristen wichtiger Mails genannt | 90,2 % | 12,5 % |
| RAG: Recall@1 / @3 | 85 % / 90 % (24 Fragen) | 75 % / 75 % (12 Fragen) |
| RAG: Antwort korrekt (Regeln) | 90 % (18 von 20) | 50 % (2 von 4) |
| RAG: Fragen ohne Antwort richtig abgelehnt | 50 % (2 von 4) | 87,5 % (7 von 8) |
| RAG: erwartete Quelle zitiert | 65 % | 0 % |
| LLM-Judge | nicht gemessen | nicht gemessen |

Geschwindigkeit (Mittel je Aufruf; „verarbeitet“ = Prompt- und Antwort-Tokens je Sekunde):

| Task | `qwen2.5:3b` | `llama3.2:1b` |
|---|---|---|
| Triage | 9,5 s (p95 12,3 s), 75 tok/s verarbeitet, 4,6 tok/s erzeugt | 3,9 s, 186 tok/s verarbeitet, 10,6 tok/s erzeugt |
| Todos | 46,3 s (Median 14,1 s, p95 120 s = Timeout) | 10,1 s (p95 23,2 s) |
| Digest | 40,2 s je Aufruf, 129 s je Digest | 17,4 s je Aufruf, 39 s je Digest |
| RAG | erstes Token nach 56 s, Antwort nach 60 s | erstes Token nach 15 s, Antwort nach 24 s |
| Embeddings (200 Mails indexieren) | 35 s | – |

### 4.2 Befunde

- **Timeouts bei der Aufgaben-Erkennung (`qwen2.5:3b`, 4 vCPUs): 30,6 % der Aufrufe.** Ohne
  Antwortlimit erzeugte das Modell bei fast jeder dritten Mail Text, bis die Frist ablief (im
  Ollama-Log rund 2 000 Tokens ohne fertiges JSON). Jeder solche Aufruf belegt den LLM-Slot für
  die volle Frist; im Betrieb hätte er ohne Frist 300 s gedauert und wäre danach wiederholt
  worden. Gemeldet als #132, seit #133 begrenzt das Gateway die Antwort (Todos: 800 Tokens) und
  die Dauer (Profil `cpu`: 180 s). Ob das die Timeouts beseitigt, misst #134; die Zahlen hier sind
  der Stand **vor** #133.
- **Triage:** `qwen2.5:3b` trennt Newsletter (86 %), Benachrichtigungen (96 %), Info (75 %) und
  „Aktion nötig“ (75 %) brauchbar, erkennt aber „Warten auf“ fast nie (1 von 24, meist als „Info“)
  und Spam kaum (3 von 20; 7 Spam-Mails, darunter Phishing und Prompt-Injection, als „wichtig“).
  `llama3.2:1b` ordnet fast alles als „Info“ ein und ist für die Triage unbrauchbar.
- **Todos:** Beide Modelle finden viele erwartete Aufgaben, erzeugen aber zwei- bis dreimal so
  viele Aufgaben wie erwartet (niedrige Precision). Fristen stimmen nur zur Hälfte.
- **Digest:** Bei `qwen2.5:3b` sind die `[n]`-Referenzen auf wichtige Mails entweder fast
  vollständig (3 Digests) oder fehlen ganz (7 Digests); die Fristen nennt der Text trotzdem fast
  immer. Die Regel misst nur Referenzen und Fristen, nicht ob der Text inhaltlich stimmt.
- **RAG:** Mit `qwen2.5:3b` findet die Hybrid-Suche die richtige Mail meist an erster Stelle, und
  die Antworten enthalten die erwarteten Fakten. Schwach ist das Ablehnen: Bei 2 von 4 Fragen ohne
  Antwort im Postfach antwortete das Modell trotzdem mit Zitat. Auf CPU dauert die erste Antwort
  rund eine Minute.

### 4.3 Empfehlung für CPU-Hosts

- Mit 4 vCPUs ist `qwen2.5:3b` für alle Tasks zusammen grenzwertig: Die Prompt-Verarbeitung
  (≈ 70–75 Tokens/s) dominiert, eine Mail braucht für Triage und Todos zusammen im Mittel fast
  eine Minute, RAG-Antworten etwa eine Minute. Für die Triage allein reicht es.
- **Antwortlimit und Frist gesetzt lassen** (#133: `OLLAMAIL_LLM_TASK_TODOS_MAX_TOKENS`,
  `OLLAMAIL_LLM_CALL_TIMEOUT`); ohne sie blockierten im Test 30 % der Todo-Aufrufe den Worker.
- **Kürzerer Kontext** (`OLLAMAIL_LLM_CONTEXT_TOKENS=4096`) senkt Speicherbedarf und die Zeit pro
  langem Prompt; die Mails des Datensatzes passen hinein. Die Wirkung auf Qualität und Timeouts
  ist **nicht gemessen** (Kandidat für #134).
- **Kein 1B-Modell für die Triage:** `llama3.2:1b` ist zwei- bis dreimal schneller, aber in der
  Triage unbrauchbar. Ein kleineres Modell kommt höchstens für einzelne Tasks infrage (Todos,
  Digest), was die Stichprobe nicht belastbar zeigt.
- Wer RAG und Todos im Alltag nutzen will, plant mehr CPU-Kerne oder eine GPU ein (Profil
  `gpu-consumer`).

### 4.4 Nicht gemessen

- **GPU-Hardware** (`gpu-consumer`, `gpu-server`): keine Messungen; die Profil-Modelle dafür
  (`qwen2.5:14b`, `qwen2.5:32b`) sind nicht bewertet.
- `bge-m3` als Embedding-Modell (nicht verfügbar), stattdessen `granite-embedding-multilingual`.
- Der LLM-Judge und das Reranking (im Profil `cpu` aus).
- RAG mit allen 60 Fragen und `llama3.2:1b` auf dem vollen Datensatz.
- Vorher/Nachher von #133: #134.
