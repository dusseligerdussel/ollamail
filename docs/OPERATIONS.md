# Betrieb

Handbuch für Admins, die ollamail selbst betreiben: Installation, Hardware, Reverse Proxy,
Backup/Restore, Updates, Datenschutz und Fehlersuche.

> **Projektstatus:** Vorbereitung auf die erste Version `v0.1.0`. Funktionsumfang und bekannte
> Einschränkungen stehen im [`CHANGELOG.md`](../CHANGELOG.md).

Referenz für Dienste, Profile, Volumes und Entwicklungsmodus: [`deploy/README.md`](../deploy/README.md).
Betrieb auf Kubernetes mit dem Helm-Chart: [`operations/kubernetes.md`](operations/kubernetes.md).
Modelle auswählen und messen: [`operations/model-evals.md`](operations/model-evals.md).
Alle Einstellungen: [`deploy/.env.example`](../deploy/.env.example).

## Inhalt

1. [Systemanforderungen](#1-systemanforderungen)
2. [Installation](#2-installation)
3. [Hardware-Profile und LLM](#3-hardware-profile-und-llm)
4. [Reverse Proxy und TLS](#4-reverse-proxy-und-tls)
5. [Backup und Restore](#5-backup-und-restore)
6. [Updates und Migrationen](#6-updates-und-migrationen)
7. [Schlüsselverwaltung](#7-schlüsselverwaltung)
8. [Skalierung](#8-skalierung)
9. [Datenschutz-Hinweise für Betreiber](#9-datenschutz-hinweise-für-betreiber)
10. [Troubleshooting](#10-troubleshooting)

## 1. Systemanforderungen

| Komponente | Anforderung |
|---|---|
| Betriebssystem | Linux-Host mit Docker Engine (amd64 oder arm64) |
| Docker | Docker Engine mit **Compose v2.24 oder neuer** (`env_file` mit `required` wird verwendet) |
| Netzwerk | Beim Bauen Zugriff auf Docker Hub, Debian-Paketquellen, PyPI und npm. Zur Laufzeit keiner nötig, außer zu Mailservern, – falls genutzt – zu einem externen LLM-Server und einmalig zu `huggingface.co` für die TTS-Stimmen (abschaltbar, siehe [3.6](#36-sprachausgabe-tts)) |
| Ports | Ein freier Port für die UI, Standard `8080` (`OLLAMAIL_HTTP_PORT`) |
| Werkzeuge | `git`, `openssl` (zum Erzeugen von Secrets) |

Der Basis-Stack (UI, API, PostgreSQL) ist schlank. Den Großteil von CPU, RAM und Speicher
benötigt später das LLM, siehe [Abschnitt 3](#3-hardware-profile-und-llm).

Die Release-Pipeline baut Multi-Arch-Images (amd64/arm64) für die GitHub Container Registry
(GHCR), siehe [`deploy/README.md`](../deploy/README.md#images). Solange es kein Release gibt und
die Pakete nicht öffentlich sind, werden die Images beim ersten Start auf dem Host gebaut (für
dessen Architektur), siehe [2.3](#23-starten).

## 2. Installation

### 2.1 Code holen

```sh
git clone https://github.com/dusseligerdussel/ollamail.git
cd ollamail
```

Alle folgenden Befehle laufen im Wurzelverzeichnis des Repositorys.

### 2.2 Konfiguration anlegen

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

In `deploy/.env` mindestens setzen:

| Variable | Wert |
|---|---|
| `OLLAMAIL_SECRET_KEY` | Zufälliger Master-Key, z. B. Ausgabe von `openssl rand -base64 32`. Verschlüsselt gespeicherte Zugangsdaten; ohne gültigen Key startet die API nicht. **Sicher aufbewahren**, siehe [Abschnitt 7](#7-schlüsselverwaltung). |
| `POSTGRES_PASSWORD` | Datenbank-Passwort, z. B. `openssl rand -hex 24`. Hex vermeidet Sonderzeichen, die in `OLLAMAIL_DATABASE_URL` URL-kodiert werden müssten. |

Wichtig:

- `POSTGRES_PASSWORD` wird nur beim **allerersten Start** (leeres Datenbank-Volume) übernommen.
  Später geänderte Werte in `.env` ändern das Passwort in der Datenbank nicht.
- `OLLAMAIL_DATABASE_URL` setzt sich per Default aus `POSTGRES_USER`, `POSTGRES_PASSWORD` und
  `POSTGRES_DB` zusammen und muss nur bei einer externen Datenbank angepasst werden.
- `deploy/.env` ist per `.gitignore` vom Commit ausgeschlossen. Die Datei enthält alle Secrets der
  Instanz – sichern, aber nicht weitergeben.

### 2.3 Starten

`deploy/compose.yaml` zieht standardmäßig fertige Images aus GHCR (`OLLAMAIL_VERSION`, Standard
`latest`). Zwei Wege:

**A – Lokal bauen** (immer möglich, nötig solange es kein Release gibt oder die GHCR-Pakete nicht
öffentlich sind):

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml up -d --build
```

Der erste Build dauert einige Minuten. `compose.build.yaml` muss dann bei **jedem**
`docker compose`-Aufruf mit angegeben werden, der Images startet (`up`, `run`, `create`); alle
anderen Befehle in diesem Dokument funktionieren auch mit nur `-f deploy/compose.yaml`.
Tipp: `export COMPOSE_FILE=deploy/compose.yaml:deploy/compose.build.yaml` in der Shell setzen und
`-f …` weglassen.

**B – Vorgebaute Images** (ab dem ersten Release, Zugriff auf die Pakete vorausgesetzt):

```sh
# in deploy/.env eine feste Version eintragen, z. B. OLLAMAIL_VERSION=0.1.0
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d
```

`docker compose -f deploy/compose.yaml up -d --build` allein baut **nicht**: `compose.yaml`
enthält keine Build-Kontexte. Ohne Zugriff auf die Images bricht der Start mit
`error from registry: unauthorized` ab, siehe [Abschnitt 10](#10-troubleshooting).

Beim Start passiert der Reihe nach:

1. `postgres` startet und wird `healthy`.
2. `migrate` führt `alembic upgrade head` aus und beendet sich (Status `Exited (0)` ist korrekt).
3. `api` und `worker` starten, nach einer gesunden `api` auch `frontend`.

### 2.4 Prüfen

```sh
docker compose -f deploy/compose.yaml ps
curl http://localhost:8080/api/healthz   # {"status":"ok"}
curl http://localhost:8080/api/readyz    # {"status":"ok","checks":{"database":"ok"}}
```

| Endpunkt | Bedeutung |
|---|---|
| `/api/healthz` | Liveness: Der API-Prozess läuft. |
| `/api/readyz` | Readiness: `200`, wenn alle Abhängigkeiten erreichbar sind, sonst `503` mit der fehlgeschlagenen Prüfung. Geprüft wird `database`, mit `OLLAMAIL_LLM_READINESS_CHECK=true` zusätzlich `llm` (alle zugewiesenen Modelle vorhanden). |

Die UI ist unter `http://<host>:8080` erreichbar. Identity-Provider (Entra ID, Google, OIDC,
LDAP/Active Directory), Rollen-Zuordnung und Nutzer verwaltet der Admin unter Admin → Anmeldung
bzw. Nutzer ([`auth/admin.md`](auth/admin.md)), ebenso GitHub ([`auth/github.md`](auth/github.md)) und SAML
([`auth/saml.md`](auth/saml.md)). Nutzer und Gruppen aus Entra ID oder Okta überträgt SCIM
(Admin → SCIM-Provisionierung, [`auth/scim.md`](auth/scim.md)).

**Erst-Admin:** Solange kein Nutzer existiert, leitet die UI auf den Setup-Assistenten (`/setup`),
der über `POST /api/setup` den ersten Admin anlegt und direkt anmeldet. Dafür
ist ein Setup-Token nötig – `OLLAMAIL_SETUP_TOKEN` oder, falls leer, ein aus `OLLAMAIL_SECRET_KEY`
abgeleiteter Wert. Die API schreibt ihn beim Start ins Log (Event `setup_pending`, Feld
`setup_code`), solange die Instanz nicht eingerichtet ist. Ist `OLLAMAIL_SETUP_TOKEN` gesetzt,
steht der Token nicht im Log. Eine der beiden Varianten genügt:

```sh
docker compose -f deploy/compose.yaml logs api | grep setup_pending
docker compose -f deploy/compose.yaml exec api python -m app.cli setup-token
```

Nach dem Setup ist der Token wertlos. Notfallzugang ohne UI (z. B. ausgesperrt oder IdP
ausgefallen): `docker compose -f deploy/compose.yaml run --rm api python -m app.cli reset-password`
(neues lokales Passwort für ein vorhandenes Konto) bzw. `… create-admin` (neues Admin-Konto).
Beide schalten eine abgeschaltete lokale Anmeldung wieder ein ([`auth/admin.md`](auth/admin.md#5-notfallzugang)).

**Cookies nur über HTTPS:** Sitzungs-Cookies sind `Secure`. Browser speichern sie über
`http://<ip>:8080` nicht (Ausnahme `http://localhost`); die Anmeldung schlägt dann fehl. Also TLS
davorsetzen oder – nur für Testinstallationen – `OLLAMAIL_AUTH_COOKIE_SECURE=false`.

Für den Betrieb im Netz unbedingt TLS davorsetzen: [Abschnitt 4](#4-reverse-proxy-und-tls).

### 2.5 Stoppen

```sh
docker compose -f deploy/compose.yaml down      # Container stoppen, Daten bleiben erhalten
```

`down -v` löscht zusätzlich **alle Volumes inklusive Datenbank**. Nur verwenden, wenn die Daten
wirklich weg sollen.

## 3. Hardware-Profile und LLM

Triage, Aufgaben, Embeddings, „Frag deine Inbox“, Antwortentwürfe und Digest nutzen ein LLM.
Standard ist das mitgelieferte Ollama (`OLLAMAIL_LLM_BASE_URL=http://ollama:11434`) mit dem
Profil `cpu`. Ohne erreichbares LLM laufen Mail-Sync und Volltextsuche trotzdem; die
KI-Schritte werden mehrfach wiederholt, Vektoren für die Suche ergänzt der Job
`search.fill_embeddings` später. Mails, deren Schritte endgültig fehlgeschlagen sind, holt
`python -m app.cli processing reprocess` nach (siehe `--help`). Modelle je Aufgabe, Profil und
Parallelität ändert der Admin zur Laufzeit unter Admin → KI.

ollamail läuft auch ohne GPU. Eine GPU beschleunigt nur, sie ist keine Voraussetzung.

### 3.1 Profile im Überblick

Richtwerte aus [`ARCHITECTURE.md`](ARCHITECTURE.md#32-llm-provider). Konkrete Modellempfehlungen
und gemessene Durchsätze folgen mit den Benchmarks aus Triage (#20); TTS siehe
[3.6](#36-sprachausgabe-tts).

| Profil | Zielhardware | Chat/Klassifikation | Betrieb |
|---|---|---|---|
| CPU-only | 16 GB RAM, keine GPU | ~1–4B-Modell, quantisiert | Compose-Profil `ollama-cpu` |
| Consumer-GPU | NVIDIA, 8–24 GB VRAM | ~7–14B-Modell | Compose-Profil `ollama-gpu` |
| Server-GPU | Rechenzentrums-GPUs | ≥ 30B-Modell, hoher Durchsatz | Externer vLLM-Server (OpenAI-kompatibel) |

Für Embeddings ist ein mehrsprachiges Modell wie `bge-m3` vorgesehen.

Standardmodelle der Profile (`OLLAMAIL_LLM_PROFILE`, überschreibbar mit
`OLLAMAIL_LLM_DEFAULT_CHAT_MODEL` / `OLLAMAIL_LLM_DEFAULT_EMBEDDING_MODEL`):

| `OLLAMAIL_LLM_PROFILE` | Chat-Modell | Embedding-Modell | Kontext |
|---|---|---|---|
| `cpu` (Standard) | `qwen2.5:3b` | `bge-m3` | 8.192 Tokens |
| `gpu-consumer` | `qwen2.5:14b` | `bge-m3` | 16.384 Tokens |
| `gpu-server` | `qwen2.5:32b` | `bge-m3` | 32.768 Tokens |

Empfehlung für CPU-only: Triage und Aufgaben-Extraktion mit einem kleinen Modell laufen lassen,
den Daily Digest in die Nacht legen.

Jeder LLM-Aufruf ist begrenzt: in der Länge der Antwort (`OLLAMAIL_LLM_MAX_OUTPUT_TOKENS`, je
Aufgabe `OLLAMAIL_LLM_TASK_<TASK>_MAX_TOKENS`) und in der Dauer (`OLLAMAIL_LLM_CALL_TIMEOUT`,
Standard im Profil `cpu` 180 s, auf GPU 60 s; Digest, „Frag deine Inbox“ und Antwortentwürfe das
Doppelte). Ein Verarbeitungsschritt, der für dieselbe Mail
`OLLAMAIL_PROCESSING_LLM_TIMEOUT_ATTEMPTS`-mal (Standard 2) in die Frist läuft, gilt als
fehlgeschlagen (`llm_timeout_error`) und blockiert den LLM-Slot nicht weiter. Häufen sich solche
Timeouts in den `llm_call`-Logs (`error_type=LLMTimeoutError`), ist das Modell für die Hardware zu
groß oder die Frist zu knapp. „Frag deine Inbox“ und Antwortentwürfe melden einen Timeout als
eigenen Fehlercode `llm_timeout` („hat zu lange gebraucht“), getrennt von `llm_unavailable`
(Server nicht erreichbar).

### 3.2 CPU-only (Profil `ollama-cpu`)

```sh
docker compose -f deploy/compose.yaml --profile ollama-cpu up -d
# Modelle des Profils `cpu` laden (einmalig, landen im Volume `ollama-models`)
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull qwen2.5:3b
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull bge-m3
```

Alternativ lädt die API fehlende Modelle beim Start selbst, wenn
`OLLAMAIL_LLM_PULL_MISSING_MODELS=true` gesetzt ist. Mit `OLLAMAIL_LLM_READINESS_CHECK=true`
meldet `/api/readyz` fehlende Modelle als `"llm":"failed"`.

Ollama ist nur im internen Compose-Netz unter `http://ollama:11434` erreichbar, der Port wird nicht
veröffentlicht. Modelle liegen im Volume `ollama-models`.

Das Profil muss bei `up` und `down` mit angegeben werden, sonst wird der Dienst nicht gestartet
bzw. nicht gestoppt (`exec` und `logs` auf einen laufenden Dienst gehen auch ohne). Alternativ
`COMPOSE_PROFILES=ollama-cpu` in der Shell oder in `deploy/.env` setzen.

### 3.3 Consumer-GPU (Profil `ollama-gpu`)

Voraussetzungen auf dem Host:

1. Aktueller NVIDIA-Treiber (`nvidia-smi` funktioniert auf dem Host).
2. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   installiert und für Docker konfiguriert.

```sh
docker compose -f deploy/compose.yaml --profile ollama-gpu up -d
docker compose -f deploy/compose.yaml --profile ollama-gpu logs ollama-gpu   # erkannte GPU prüfen
docker compose -f deploy/compose.yaml exec ollama-gpu ollama pull <modell>
```

Der Container reserviert alle GPUs des Hosts. Nur eines der beiden Ollama-Profile gleichzeitig
starten – beide melden sich im Netz als `ollama`.

### 3.4 Server-GPU mit vLLM

vLLM (oder LM Studio, LocalAI, llama.cpp-Server) läuft als eigener Dienst (eigener Host oder
eigene Compose-Datei, nicht Teil von `deploy/compose.yaml`) und wird über seine
OpenAI-kompatible API angebunden:

```sh
OLLAMAIL_LLM_PROVIDER=openai_compatible
OLLAMAIL_LLM_BASE_URL=http://<vllm-host>:8000/v1
OLLAMAIL_LLM_PROFILE=gpu-server
```

Weitere Endpunkte (z. B. Embeddings weiter über Ollama) per `OLLAMAIL_LLM_ENDPOINTS` und
`OLLAMAIL_LLM_TASK_<AUFGABE>_ENDPOINT`, siehe `deploy/.env.example` und
[`ARCHITECTURE.md`](ARCHITECTURE.md#32-llm-provider).

### 3.5 Externer Ollama-Server

Ohne Ollama-Profil kann ein vorhandener Ollama-Server genutzt werden:

- Anderer Host: `OLLAMAIL_LLM_BASE_URL=http://<host>:11434`
- Ollama direkt auf dem Docker-Host: `OLLAMAIL_LLM_BASE_URL=http://host.docker.internal:11434`.
  Unter Linux braucht der Container dafür einen `extra_hosts`-Eintrag
  (`host.docker.internal:host-gateway`) in einer eigenen Compose-Override-Datei.

Cloud-LLMs sind standardmäßig gesperrt (`OLLAMAIL_LLM_CLOUD_ENABLED=false`), siehe
[Abschnitt 9](#9-datenschutz-hinweise-für-betreiber).

### 3.6 Sprachausgabe (TTS)

Standard ist **Piper** (ADR 4), Teil des Backend-Images; ffmpeg kodiert nach Opus und MP3. Die
Stimmen sind nicht im Image: Der Worker lädt fehlende Standardstimmen beim ersten Bedarf und
täglich per Job `tts.ensure_voices` (Queue `tts`) ins Daten-Volume nach
`/data/tts/voices/piper/` (je Stimme `<id>.onnx` und `<id>.onnx.json`, 60–120 MB).

| Sprache | Standardstimme | Datensatz / Lizenz |
|---|---|---|
| Deutsch | `de_DE-thorsten-medium` (`OLLAMAIL_TTS_VOICE_DE`) | Thorsten-Voice, CC0-1.0 |
| Englisch | `en_US-ljspeech-medium` (`OLLAMAIL_TTS_VOICE_EN`) | LJ Speech, Public Domain |

Andere Stimmen aus [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) lassen
sich über die Variablen einstellen. Vorher die Lizenz im `MODEL_CARD` der Stimme prüfen: Einige
Trainingsdatensätze haben Nutzungsbeschränkungen (z. B. nur nicht-kommerziell).

**Ohne Internetzugang:** `OLLAMAIL_TTS_DOWNLOAD_VOICES=false` setzen und beide Dateien je Stimme
manuell ins Volume kopieren:

```sh
docker compose -f deploy/compose.yaml cp de_DE-thorsten-medium.onnx worker:/data/tts/voices/piper/
docker compose -f deploy/compose.yaml cp de_DE-thorsten-medium.onnx.json worker:/data/tts/voices/piper/
```

**Durchsatz** (gemessen in #27, Intel Xeon 2,1 GHz, 4 Kerne, CPU-only, englische Stimme der
Qualität `medium`; die Standardstimmen haben dieselbe Modellgröße):
2.000 Zeichen Text ergeben rund 160 s Audio und brauchen inklusive Normalisierung und
Kodierung nach Opus und MP3 etwa 9–10 s, also rund 6 % der Echtzeit. Die Synthese läuft pro
Worker-Prozess nacheinander und nutzt dabei alle Kerne.

### 3.7 Suchindex und Embedding-Modell

Jede neue Mail durchläuft den Verarbeitungsschritt `index` (Queue `llm`): Mailtext (ohne Zitate
und Signatur) und Text aus Anhängen (PDF, DOCX, TXT, HTML; Scans per OCR, siehe
[3.8](#38-ocr-für-gescannte-anhänge)) werden in Abschnitte
(„Chunks“) zerlegt, in PostgreSQL volltextindiziert und mit dem Embedding-Modell
(Standard `bge-m3`, Aufgabe `embeddings`) in Vektoren umgerechnet. Die Suche kombiniert beide
Indizes. Ist das LLM beim Indizieren nicht erreichbar, ist die Mail trotzdem sofort per Volltext
auffindbar; der Job `search.fill_embeddings` (alle 5 Minuten, hinter neuen Mails) ergänzt die
Vektoren später.

Anhänge werden in einem eigenen Prozess ohne Umgebungsvariablen und mit Grenzen für Größe,
Laufzeit und Speicher gelesen (`OLLAMAIL_SEARCH_ATTACHMENT_MAX_BYTES`,
`OLLAMAIL_SEARCH_EXTRACTION_TIMEOUT`, `OLLAMAIL_SEARCH_EXTRACTION_MAX_MEMORY_MB`).

**Stand prüfen:**

```sh
docker compose -f deploy/compose.yaml run --rm api python -m app.cli search status
```

zeigt Anzahl der Chunks, Vektoren je Modell, das aktive und das konfigurierte Modell sowie die
Vektor-Dimension.

**Modellwechsel mit gleicher Dimension** (z. B. ein anderes Modell mit 1024 Dimensionen):
`OLLAMAIL_LLM_TASK_EMBEDDINGS_MODEL` (oder `OLLAMAIL_LLM_DEFAULT_EMBEDDING_MODEL`) setzen und
Worker neu starten. Der Job `search.fill_embeddings` berechnet die Vektoren aller Chunks im
Hintergrund neu (`OLLAMAIL_SEARCH_REEMBED_BATCH_SIZE` je Durchlauf, neueste Mails zuerst).
Bis er fertig ist, beantworten die Vektoren des **alten** Modells die Suchanfragen; das alte
Modell muss dafür auf dem Endpunkt installiert bleiben. Danach schaltet der Job um und löscht die
alten Vektoren. Während des Wechsels werden neue Mails mit beiden Modellen eingebettet.

**Modellwechsel mit anderer Dimension** (z. B. von 1024 auf 768): Die Vektorspalte hat eine feste
Länge, alte und neue Vektoren können nicht nebeneinander liegen.

1. Neues Modell und `OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS=<n>` in `deploy/.env` setzen (höchstens
   2000, Grenze des HNSW-Index).
2. Worker stoppen: `docker compose -f deploy/compose.yaml stop worker`.
3. Spalte umstellen: `docker compose -f deploy/compose.yaml run --rm api python -m app.cli search resize`.
   Das löscht alle Vektoren, ändert die Spalte auf `vector(<n>)` und legt den HNSW-Index neu an
   (in einer Transaktion).
4. Worker starten. `search.fill_embeddings` baut die Vektoren im Hintergrund neu auf. Bis dahin
   findet die Suche Mails nur per Volltext bzw. mit den schon neu berechneten Vektoren.

Eine Neu-Indizierung inklusive Chunking (z. B. nach geänderter Chunk-Größe) startet
`python -m app.cli processing reprocess --step index`.

### 3.8 OCR für gescannte Anhänge

Gescannte PDFs (Seiten ohne Textlayer) und auf Wunsch Bilder werden lokal mit Tesseract erkannt
und landen als Chunks mit der Quelle „Anhang (OCR)“ im Suchindex und in den Zitaten von „Frag
deine Inbox“. Tesseract samt deutschen und englischen Sprachdaten ist im Backend-Image enthalten
(amd64 und arm64); es werden keine Daten nachgeladen.

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `OLLAMAIL_SEARCH_OCR_MODE` | `pdf` | `off` (kein OCR), `pdf` (nur PDF-Seiten ohne Text), `all` (zusätzlich PNG, JPEG, TIFF; Inline-Bilder wie Logos nie) |
| `OLLAMAIL_SEARCH_OCR_MAX_PAGES` | `20` | Höchstens so viele Seiten je PDF werden erkannt; Seiten mit Textlayer zählen nicht |
| `OLLAMAIL_SEARCH_OCR_LANGUAGES` | `deu+eng` | Tesseract-Sprachen; weitere nur mit eigenem Image (`tesseract-ocr-<lang>`) |
| `OLLAMAIL_SEARCH_OCR_TIMEOUT` | `300` | Sekunden je Anhang; danach wird der Prozess samt Tesseract beendet (Status `timeout`) |
| `OLLAMAIL_SEARCH_OCR_CONCURRENCY` | `1` | Parallele OCR-Jobs je Worker; jeder belegt einen CPU-Kern |

**Ablauf:** Beim Indizieren einer Mail wird der Textlayer eines PDFs sofort indiziert. Hat das
PDF Seiten ohne Text, aber mit Bild, kommt ein Job `search.ocr_attachment` auf die eigene Queue
`ocr` (eigene Job-Slots, niedrigste Priorität). OCR blockiert damit weder den Mail-Sync noch die
LLM-Verarbeitung neuer Mails. Der Job ersetzt die Chunks des Anhangs durch Textlayer plus
erkannten Text; die Vektoren ergänzt `search.fill_embeddings`. Fehler und Timeouts erscheinen nur
als Statuscode im Log (`search_attachment_ocr`, `status=timeout|ocr_failed|...`), der Textlayer
bleibt dann im Index. Die Queue `ocr` muss von einem Worker abgearbeitet werden
(`OLLAMAIL_WORKER_QUEUES`, Standard enthält `ocr`). Wer `OLLAMAIL_WORKER_QUEUES` explizit setzt,
muss `ocr` ergänzen, sonst bleiben die Jobs liegen.

**Bestehende Mails** nachträglich erkennen: `python -m app.cli processing reprocess --step index`
(stellt für jeden Scan wieder einen OCR-Job ein).

**Durchsatz** (`backend/scripts/ocr_benchmark.py`, synthetische A4-Seiten mit ~3.300 Zeichen,
300 dpi, Intel Xeon 2,1 GHz, 4 Kerne, Tesseract 5.3.4 ohne OpenMP, LSTM-Modelle `deu+eng`):

| `OLLAMAIL_SEARCH_OCR_CONCURRENCY` | Seiten pro Minute | Sekunden pro Seite |
|---|---|---|
| 1 | 18,6 | 3,2 |
| 2 | 36,8 | 1,6 |
| 4 | 72,8 | 0,8 |

Die Seiten sind sauber gerendert (Wortgenauigkeit 100 %); echte Scans (Rauschen, Schräglage,
kleinere Schrift) sind langsamer und ungenauer. Richtwerte je Hardware-Profil:

| Profil | Empfehlung |
|---|---|
| CPU-only (4 Kerne) | `OLLAMAIL_SEARCH_OCR_CONCURRENCY=1` (Standard): rund 1.000 Seiten pro Stunde, drei Kerne bleiben für Sync, LLM und TTS. Beim Erstimport großer Postfächer ggf. nachts auf `2` erhöhen |
| Consumer-GPU / Server | Tesseract nutzt keine GPU. `OLLAMAIL_SEARCH_OCR_CONCURRENCY` = freie CPU-Kerne, oder ein eigener Worker-Container nur mit `OLLAMAIL_WORKER_QUEUES=ocr` |

Speicher: Tesseract braucht je A4-Seite (300 dpi) rund 100 MB (gemessen 93 MB);
`OLLAMAIL_SEARCH_EXTRACTION_MAX_MEMORY_MB` (Standard 1024) begrenzt Kindprozess und Tesseract
jeweils einzeln.
Image-Größe: Tesseract mit `deu`/`eng` und Abhängigkeiten rund 12 MB, `pypdfium2` (rendert
PDF-Seiten) rund 8,5 MB.

## 4. Reverse Proxy und TLS

Der `frontend`-Container spricht nur HTTP auf Port 8080 und ist der einzige veröffentlichte Port.
TLS terminiert ein vorgeschalteter Reverse Proxy. Dafür in `deploy/.env`:

```sh
OLLAMAIL_HTTP_BIND=127.0.0.1   # UI nur noch lokal erreichbar, nicht direkt aus dem Netz
```

und den Stack neu starten (`docker compose -f deploy/compose.yaml up -d`).

Anforderungen an den Proxy:

- **Gesamten Pfad** (`/`) an `http://127.0.0.1:8080` weiterleiten. UI und API (`/api`) laufen über
  denselben Origin; die Content-Security-Policy erlaubt keine fremden Origins.
- `Host`, `X-Forwarded-For` und `X-Forwarded-Proto` setzen. Der interne Caddy übernimmt
  `X-Forwarded-*` nur von privaten Netzen (RFC 1918, Loopback); ein Proxy auf demselben Host oder
  im selben LAN erfüllt das.
- **Server-Sent Events** (Live-Updates, gestreamte Antworten von „Frag deine Inbox“):
  Antwort-Pufferung abschalten und lange Verbindungen erlauben. Der interne Caddy nutzt dafür
  bereits `flush_interval -1` und 1 h Timeout – der äußere Proxy muss mindestens genauso großzügig
  sein.

Die folgenden Beispiele verwenden `mail.example.org` als Hostnamen.

### 4.1 Caddy

Caddy holt sich das Zertifikat automatisch über Let's Encrypt (Ports 80/443 müssen erreichbar sein).

```caddyfile
mail.example.org {
	reverse_proxy 127.0.0.1:8080 {
		# Server-Sent Events sofort durchreichen
		flush_interval -1
	}
}
```

Caddy setzt `X-Forwarded-*` automatisch.

### 4.2 nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name mail.example.org;

    ssl_certificate     /etc/ssl/mail.example.org/fullchain.pem;
    ssl_certificate_key /etc/ssl/mail.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        # Server-Sent Events: nicht puffern, lange Verbindungen zulassen
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}

server {
    listen 80;
    server_name mail.example.org;
    return 301 https://$host$request_uri;
}
```

`http2 on;` benötigt nginx 1.25.1 oder neuer; bei älteren Versionen stattdessen
`listen 443 ssl http2;`. Zertifikate z. B. per certbot.

### 4.3 Traefik

Beispiel mit File-Provider (dynamische Konfiguration), Traefik v3. Der Entrypoint `websecure` und
ein Certificate Resolver (hier `letsencrypt`) sind in der statischen Traefik-Konfiguration definiert.

```yaml
http:
  routers:
    ollamail:
      rule: Host(`mail.example.org`)
      entryPoints: [websecure]
      service: ollamail
      tls:
        certResolver: letsencrypt
  services:
    ollamail:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8080
        responseForwarding:
          # Server-Sent Events sofort durchreichen
          flushInterval: -1
```

Läuft Traefik selbst als Container, ist `127.0.0.1` dort der Traefik-Container. Dann die
IP-Adresse des Docker-Hosts eintragen und `OLLAMAIL_HTTP_BIND` auf diese Adresse setzen – oder
Traefik und ollamail in ein gemeinsames Docker-Netz hängen.

### 4.4 Sicherheits-Header

Der interne Caddy setzt bereits CSP, `X-Frame-Options`, `Referrer-Policy` u. a.
(Details: [`deploy/README.md`](../deploy/README.md#sicherheits-header)). Der äußere Proxy sollte diese
nicht überschreiben. HSTS (`Strict-Transport-Security`) setzt ollamail nicht, da es TLS nicht
selbst terminiert – bei Bedarf im äußeren Proxy ergänzen.

## 5. Backup und Restore

Ein vollständiges Backup besteht aus **drei Teilen**:

| Teil | Inhalt | Ohne ihn … |
|---|---|---|
| `deploy/.env` | `OLLAMAIL_SECRET_KEY`, Datenbank-Passwort, alle Einstellungen | … sind verschlüsselt gespeicherte Zugangsdaten (ab #6) **unwiederbringlich verloren** |
| Datenbank | Alle Anwendungsdaten (Volume `postgres-data`) | … ist die Instanz leer |
| Daten-Volume | Anhänge, Audio-Digests (Volume `ollamail-data`, in den Containern `/data`) | … fehlen Dateien |

Das Volume `ollama-models` muss nicht gesichert werden; Modelle lassen sich neu laden.

> **Den Secret Key getrennt vom Datenbank-Backup aufbewahren** (z. B. im Passwort-Manager oder
> Tresor). Wer Backup und Key zusammen hat, kann die gespeicherten Zugangsdaten entschlüsseln.

Volumes heißen auf dem Host `<projekt>_<volume>`, mit dem Standard-Projektnamen also
`ollamail_postgres-data` und `ollamail_ollamail-data` (`docker volume ls`).

### 5.1 Backup erstellen

Im Wurzelverzeichnis des Repositorys, bei laufendem Stack. Das Zielverzeichnis liegt bewusst
**außerhalb** des Repositorys, damit Backups nicht versehentlich committet werden:

```sh
BACKUP_DIR=/var/backups/ollamail   # beliebig, außerhalb des Repositorys
mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"

# 1. Konfiguration inkl. Secret Key
cp deploy/.env "$BACKUP_DIR/ollamail.env"

# 2. Datenbank (konsistenter Snapshot, auch im laufenden Betrieb)
docker compose -f deploy/compose.yaml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$BACKUP_DIR/ollamail-db.dump"

# 3. Daten-Volume
docker run --rm -v ollamail_ollamail-data:/data:ro -v "$BACKUP_DIR":/backup alpine:3 \
  tar czf /backup/ollamail-data.tar.gz -C /data .
```

Hinweise:

- Das Backup-Verzeichnis enthält Secrets und Mail-Daten: verschlüsselt und zusätzlich außerhalb
  des Hosts aufbewahren.
- Das Daten-Volume wird im laufenden Betrieb gesichert. Für ein exakt zur Datenbank passendes
  Backup vorher `docker compose -f deploy/compose.yaml stop api worker` und danach wieder
  `start api worker` ausführen.
- Regelmäßig per cron/systemd-Timer ausführen und mindestens einmal einen Restore-Test machen.

### 5.2 Restore auf einer leeren Instanz

Gilt für einen neuen Host oder nach `down -v`. Voraussetzung: Repository auf derselben oder einer
neueren Version wie beim Backup (siehe [Abschnitt 6](#6-updates-und-migrationen)).

```sh
BACKUP_DIR=/var/backups/ollamail

# 1. Konfiguration zurückspielen (Secret Key und Passwort müssen zum Backup passen)
cp "$BACKUP_DIR/ollamail.env" deploy/.env
chmod 600 deploy/.env

# 2. Container und Volumes anlegen, nur die Datenbank starten (leeres Volume)
docker compose -f deploy/compose.yaml create
docker compose -f deploy/compose.yaml up -d --wait postgres

# 3. Datenbank einspielen
docker compose -f deploy/compose.yaml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$BACKUP_DIR/ollamail-db.dump"

# 4. Daten-Volume einspielen
docker run --rm -v ollamail_ollamail-data:/data -v "$BACKUP_DIR":/backup:ro alpine:3 \
  sh -c 'tar xzf /backup/ollamail-data.tar.gz -C /data && chown -R 10001:10001 /data'

# 5. Rest starten (migrate bringt das Schema ggf. auf den aktuellen Stand)
docker compose -f deploy/compose.yaml up -d --wait
curl http://localhost:8080/api/readyz
```

`--exit-on-error` bricht beim ersten Fehler ab. Ein Fehler in Schritt 3 bedeutet meist, dass die
Datenbank nicht leer war – Restore immer in ein **leeres** Volume.

Das Daten-Volume gehört dem App-Nutzer mit UID 10001 (`chown` in Schritt 4).

### 5.3 Restore-Test ohne Produktionsdaten zu berühren

Ein zweiter Compose-Projektname (`-p`) erzeugt eigene Container und Volumes neben der laufenden
Instanz. Auf demselben Host einen anderen UI-Port wählen:

```sh
BACKUP_DIR=/var/backups/ollamail
T="docker compose -p ollamail-restoretest -f deploy/compose.yaml"

$T create
$T up -d --wait postgres
$T exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$BACKUP_DIR/ollamail-db.dump"
OLLAMAIL_HTTP_PORT=8081 $T up -d --wait
curl http://localhost:8081/api/readyz

# Aufräumen: löscht nur die Test-Instanz und ihre Volumes
$T down -v
```

Für einen vollständigen Test auch das Daten-Volume einspielen (wie in 5.2, Volume-Name
`ollamail-restoretest_ollamail-data`).

## 6. Updates und Migrationen

Vor **jedem** Update ein vollständiges Backup ziehen ([5.1](#51-backup-erstellen)): Migrationen
lassen sich nicht automatisch zurückdrehen, das Backup ist der einzige Weg zurück.

### 6.1 Update mit vorgebauten Images

```sh
# 1. Backup (Abschnitt 5.1)

# 2. Neue Version in deploy/.env eintragen, z. B. OLLAMAIL_VERSION=0.2.0
#    (Release-Notes lesen: neue oder geänderte Einstellungen, Hinweise zum Upgrade)

# 3. Images holen und neu starten
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d --wait
curl http://localhost:8080/api/readyz
```

Mit `OLLAMAIL_VERSION=latest` holt `pull` jeweils das neueste stabile Release. Für
reproduzierbare Installationen eine feste Version eintragen; `edge` ist ungetestet und nicht für
den Produktivbetrieb.

### 6.2 Update bei lokalem Build

```sh
# 1. Backup (Abschnitt 5.1)

# 2. Neue Version holen (Tag oder main)
git fetch --tags
git checkout v0.2.0

# 3. Neu bauen und starten
docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml up -d --build --wait
curl http://localhost:8080/api/readyz
```

### 6.3 Migrationen und Konfiguration

Datenbank-Migrationen laufen **automatisch**: Der Dienst `migrate` führt vor jedem Start von
`api`/`worker` `alembic upgrade head` aus. Schlägt eine Migration fehl, starten `api`, `worker`
und `frontend` nicht; die Ursache steht in `docker compose -f deploy/compose.yaml logs migrate`.
Alle ausstehenden Migrationen laufen in einer Transaktion; schlägt eine fehl, bleibt die
Datenbank auf dem alten Stand. Zurück geht es dann wie in [6.4](#64-rollback-auf-die-vorherige-version).

Aktuellen Schema-Stand anzeigen:

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps migrate alembic current
```

Neue Variablen in `deploy/.env.example` nach jedem Update mit der eigenen `deploy/.env`
vergleichen, z. B. mit `diff deploy/.env.example deploy/.env`. Fehlende Variablen fallen auf den
Standardwert aus dem Code zurück.

### 6.4 Rollback auf die vorherige Version

Einen Downgrade-Befehl gibt es nicht. Eine ältere Version kann ein neueres Schema nicht lesen;
zurück geht es nur mit dem Backup von **vor** dem Update:

```sh
# 1. Stack stoppen
docker compose -f deploy/compose.yaml down

# 2. Alte Version wählen: OLLAMAIL_VERSION in deploy/.env zurücksetzen
#    bzw. bei lokalem Build `git checkout <alte-version>`

# 3. Datenbank-Volume leeren (löscht die aktuelle Datenbank!) und Backup einspielen,
#    wie in Abschnitt 5.2 ab Schritt 2
docker volume rm ollamail_postgres-data
```

Änderungen seit dem Backup (neue Mails, Aufgaben, Einstellungen) gehen dabei verloren; neue Mails
holt der Sync beim nächsten Start erneut vom Mailserver.

### 6.5 Drittanbieter-Images

PostgreSQL (`POSTGRES_IMAGE`, Standard `pgvector/pgvector:pg16`) und Ollama (`OLLAMA_IMAGE`) sind
über Variablen in `deploy/.env` festgelegt. Ein Wechsel der **PostgreSQL-Hauptversion** (z. B. 16 → 17)
ist mit dem bestehenden Volume nicht möglich: Backup ziehen, neue Version mit leerem Volume
starten, Backup einspielen (Abschnitt 5).

## 7. Schlüsselverwaltung

`OLLAMAIL_SECRET_KEY` ist der Master-Key für die Verschlüsselung gespeicherter Zugangsdaten
(Postfach-Passwörter, OAuth-Tokens, IdP-Secrets; Envelope-Encryption mit AES-256-GCM, Verfahren
siehe [`PRIVACY.md`](PRIVACY.md)). Ohne gültigen Key startet `api` nicht.

- Geht der Key verloren, sind gespeicherte Zugangsdaten nicht mehr lesbar. Postfächer und
  Identity-Provider müssen dann neu eingerichtet werden.
- Den Key nach der Einrichtung nicht einfach in `.env` austauschen, sondern rotieren.

**Key-Rotation:** Ablauf mit `OLLAMAIL_SECRET_KEYS_OLD` und `python -m app.cli rotate-keys` in
[`deploy/README.md`](../deploy/README.md#master-key-und-key-rotation).

## 8. Skalierung

Es läuft eine API-Instanz. Der `worker` (Procrastinate, Queue in PostgreSQL) übernimmt Mail-Sync,
KI-Verarbeitung, OCR und TTS und startet immer mit. Die Jobs sind nach Typ auf Queues verteilt
(`sync`, `llm`, `tts`, `ocr`, `default`); `OLLAMAIL_WORKER_QUEUES` legt fest, welche ein Worker
abarbeitet, sodass z. B. ein eigener Worker nur LLM-Jobs auf einem GPU-Host übernimmt. Mehr
Instanzen: `docker compose -f deploy/compose.yaml up -d --scale worker=2`, Details in
[`deploy/README.md`](../deploy/README.md#worker-skalieren).

**Mail-Sync (IMAP):** Jeder Worker, der die Queue `sync` abarbeitet, hält zusätzlich eine
Datenbankverbindung für die Verteilung der Postfächer (Advisory-Locks) und pro überwachtem
Postfach eine dauerhafte IMAP-Verbindung (`IDLE`); während eines Syncs kommt eine zweite hinzu.
Mailserver begrenzen gleichzeitige Verbindungen pro Nutzer (Dovecot: `mail_max_userip_connections`,
Standard 10) – zwei pro Postfach reichen. Laufen mehrere Worker mit `sync`, übernimmt jeder einen
Teil der Postfächer; fällt einer aus, übernehmen die anderen innerhalb einer Minute.
Mailserver mit selbstsigniertem Zertifikat: das CA-Zertifikat dem Container über
`SSL_CERT_FILE` bekannt machen; `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true` (keine Prüfung,
auch unverschlüsselt) nur in Testumgebungen.

**Mail-Sync (Microsoft 365):** Keine dauerhafte Verbindung; der Worker pollt per Delta Query
(`poll_interval_seconds`, Standard 5 Minuten). Change Notifications sind optional und brauchen
eine öffentlich erreichbare URL (`OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL`). Einrichtung der
Entra-App, Berechtigungen und Einschränkung von App-only-Zugriff:
[`docs/providers/microsoft365.md`](providers/microsoft365.md).

**Mail-Sync (Gmail / Google Workspace):** Einrichtung (OAuth-Client, Scopes, Google-Verifizierung,
Domain-wide Delegation, optional Pub/Sub) und manuelle Testanleitung in
[`providers/gmail.md`](providers/gmail.md). Es ist keine öffentliche URL nötig: Der
OAuth-Redirect muss nur vom Browser erreichbar sein, Änderungen holt der Worker standardmäßig per
Polling (`poll_interval_seconds`, Standard 5 Minuten) über die Gmail-API ab. Ausgehend braucht der
Worker HTTPS zu `gmail.googleapis.com` und `oauth2.googleapis.com` (bei Pub/Sub zusätzlich
`pubsub.googleapis.com`). Die Service-Account-Schlüsseldatei (`OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE`)
als Docker-Secret einbinden, nie ins Image oder Repo.

Was heute schon gilt: Jeder API- bzw. Worker-Prozess öffnet bis zu
`OLLAMAIL_DATABASE_POOL_SIZE + OLLAMAIL_DATABASE_MAX_OVERFLOW` Datenbankverbindungen (Standard
5 + 10). Beim Hochskalieren darauf achten, dass die Summe unter `max_connections` von PostgreSQL
bleibt (PostgreSQL-Standard: 100).

## 9. Datenschutz-Hinweise für Betreiber

Grundlage für Verarbeitungsverzeichnis und DSFA. Grundsätze und technische Maßnahmen:
[`PRIVACY.md`](PRIVACY.md). Die Spalte „Status“ zeigt, was die aktuelle Version tatsächlich
verarbeitet. Die vollständige Liste aller Tabellen und Dateien mit Löschweg steht in
[`PRIVACY.md`](PRIVACY.md#tabellen-und-speicherorte-grundlage-für-das-verarbeitungsverzeichnis).

### 9.1 Datenkategorien und Speicherorte

| Datenkategorie | Speicherort | Status |
|---|---|---|
| Nutzerkonten, Rollen | PostgreSQL (`postgres-data`): `users`, `auth_identities` (Passwörter als Argon2id-Hash) | aktiv |
| Gruppen | PostgreSQL: `auth_identities.groups` (Gruppen-Claims des IdP beim letzten Login, z. B. Entra-Gruppen-IDs) | aktiv (OIDC, SAML, GitHub-Teams als `<org>/<team>`); LDAP liest Gruppen bei jedem Login und speichert sie nicht |
| IdP-Konfiguration (OIDC) | PostgreSQL: `auth_oidc_providers` (Client-Secret verschlüsselt mit `OLLAMAIL_SECRET_KEY`) oder Umgebung (`OLLAMAIL_AUTH_OIDC_PROVIDERS`) | aktiv |
| Sessions | PostgreSQL: `auth_sessions` (nur SHA-256 des Cookie-Tokens, Browser-Kennung gekürzt); abgelaufene stündlich gelöscht | aktiv |
| Login-Zähler (Rate-Limit, Sperre) | PostgreSQL: `auth_rate_limits` (nur HMAC von IP bzw. E-Mail-Adresse); stündlich bereinigt | aktiv |
| Postfach-Zugangsdaten, OAuth-Tokens, IdP-Secrets | PostgreSQL (`mail_mailboxes.credentials`, Provider-Tabellen), verschlüsselt mit `OLLAMAIL_SECRET_KEY` | aktiv |
| E-Mails (Header, Inhalte, Metadaten), Threads, Ordner | PostgreSQL (`mail_messages`, `mail_threads`, `mail_folders`, …) | aktiv |
| Anhänge | Daten-Volume (`ollamail-data`), Metadaten in `mail_attachments` | aktiv |
| Triage: Kategorie, Priorität, Begründung (ein Satz) je Mail | PostgreSQL (`triage_results`), gelöscht mit der Mail | aktiv |
| Triage-Korrekturen (Few-Shot-Beispiele, nur für denselben Nutzer; Embeddings als Zahlenvektor) | PostgreSQL (`triage_feedback`), gelöscht mit Mail oder Nutzer | vorhanden (#20) |
| Kategorien, Absenderregeln (Adresse oder Domain), Einstellungen je Nutzer/Postfach | PostgreSQL (`triage_categories`, `triage_category_preferences`, `triage_sender_rules`, `triage_mailbox_settings`) | vorhanden (#20) |
| KI-Ergebnisse: Aufgaben | PostgreSQL (`todos`) | aktiv |
| Suchindex: Text-Abschnitte von Mails und Anhängen, Volltextindex, Embeddings | PostgreSQL: `search_chunks`, `search_embeddings` (pgvector); hängen per `ON DELETE CASCADE` an Mail, Anhang und Postfach | vorhanden (#24) |
| Chat-Verläufe („Frag deine Inbox“) | PostgreSQL (`rag_conversations`, `rag_messages`, `rag_citations`) | aktiv |
| Daily Digest: Text und Audio | PostgreSQL (`digests`, `digest_user_settings`) bzw. Daten-Volume; nach `OLLAMAIL_DIGEST_RETENTION_DAYS` gelöscht | aktiv |
| Antwortentwürfe (Empfänger, Betreff, Text, Anweisung), Signatur | PostgreSQL: `reply_drafts`, `reply_draft_settings`; gelöscht mit Nutzer, Postfach oder nach `OLLAMAIL_DRAFTS_RETENTION_DAYS` ohne Änderung (Job `drafts.purge`) | aktiv (#92) |
| Audit-Log (Ereignistyp, Zeitpunkt, Nutzer- bzw. Objekt-ID, Codes und Zähler; keine Inhalte, Betreffzeilen oder Adressen) | PostgreSQL: `audit_events`, append-only; Aufbewahrung über Admin → Aufbewahrung bzw. `OLLAMAIL_AUDIT_RETENTION_DAYS` (Job `privacy.retention`) | aktiv |
| Datenexporte der Nutzer (ZIP mit allen eigenen Daten) | PostgreSQL: `privacy_exports`; Daten-Volume `exports/<user_id>/`; nach `OLLAMAIL_PRIVACY_EXPORT_EXPIRY_HOURS` gelöscht | aktiv (#36) |
| Aufbewahrungsfristen | PostgreSQL: `privacy_retention_settings` (keine personenbezogenen Daten) | aktiv (#36) |
| Aufgaben-Export: Ziel, Server-URL, Benutzername, Passwort bzw. Google-Refresh-Token (verschlüsselt), Liste, Modus je Nutzer; Verweise auf die exportierten Aufgaben | PostgreSQL: `todo_export_targets` (Zugangsdaten verschlüsselt mit `OLLAMAIL_SECRET_KEY`), `todos.external_refs` | aktiv, nur mit `OLLAMAIL_TODOS_EXPORT_SINKS` (#40, Microsoft To Do #101, Google Tasks #102) |
| Job-Queue (nur IDs und Parameter, keine Mail-Inhalte) | PostgreSQL (`procrastinate_*`) | aktiv |
| Verarbeitungsstatus je Mail und Schritt (Version, Status, Fehlercode; keine Inhalte) | PostgreSQL (`message_processing`) | vorhanden (#19) |
| LLM-Modelle (keine personenbezogenen Daten) | Volume `ollama-models` | vorhanden (Profil `ollama-*`) |
| TTS-Stimmen (keine personenbezogenen Daten) | Daten-Volume, `tts/voices/<engine>/` | vorhanden (#27) |
| Instanz-Secrets und Konfiguration | `deploy/.env` auf dem Host | vorhanden |
| Betriebslogs (ohne Mail-Inhalte, siehe 9.3) | Docker-Logging des Hosts | vorhanden |

Aufbewahrungsfristen (Admin → Aufbewahrung), Datenexport und Kontolöschung (Einstellungen →
Deine Daten) sind aktiv (#36); Details in [`PRIVACY.md`](PRIVACY.md#betroffenenrechte--löschkonzept).

### 9.2 Datenflüsse

```
Browser ──HTTPS──▶ Reverse Proxy ──HTTP──▶ frontend (Caddy) ──▶ api ──▶ PostgreSQL
                                                                   ▲
                       worker ─────────────────────────────────────┘
                         │
                         ├──▶ Mailserver: IMAP (#14) / Gmail API (#38) / Microsoft Graph (#37)
                         ├──▶ LLM: Ollama im Compose-Netz oder eigener Server
                         ├──▶ huggingface.co: Download fehlender TTS-Stimmen, sendet keine Daten (#27)
                         └──▶ Cloud-LLM nur bei OLLAMAIL_LLM_CLOUD_ENABLED=true

worker ──▶ CalDAV-Server des Nutzers: nur mit OLLAMAIL_TODOS_EXPORT_SINKS=caldav (#40)
worker ──▶ tasks.googleapis.com / oauth2.googleapis.com: nur mit OLLAMAIL_TODOS_EXPORT_SINKS=gtasks (#102)
worker ──▶ graph.microsoft.com (Microsoft To Do): nur mit OLLAMAIL_TODOS_EXPORT_SINKS=mstodo (#101)

api ──▶ Identity-Provider: LDAP/AD (LDAPS/StartTLS, #32), OIDC (#30), GitHub OAuth2 (#31), SAML 2.0 (#94; Metadaten-URL des IdP)
api ──▶ login.microsoftonline.com / Graph: nur beim Verbinden eines Microsoft-365-Postfachs (#37)
api ──▶ Mailserver: SMTP (IMAP-Postfächer) / Gmail API / Graph – nur wenn ein Nutzer eine Antwort sendet (#92)
Microsoft ──▶ api: Change Notifications nur mit OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL (#37)
```

- **Standardmäßig verlassen keine Daten die Instanz.** Externe Verbindungen entstehen nur zu den
  Mailservern und Identity-Providern, die der Admin konfiguriert, und zu einem LLM-Server, den er
  einträgt.
- **Cloud-LLMs** sind ein globaler Admin-Schalter, Standard `OLLAMAIL_LLM_CLOUD_ENABLED=false`.
  Wird er aktiviert, gehen Mail-Inhalte an den jeweiligen Anbieter. Das ist dann eine
  Auftragsverarbeitung bzw. Drittlandübermittlung, die der Betreiber vertraglich absichern muss.
- **Aufgaben-Export** (#40) ist ein Admin-Opt-in: Standard `OLLAMAIL_TODOS_EXPORT_SINKS=`
  (leer, aus). Mit `caldav` darf jeder Nutzer unter Einstellungen → Aufgaben-Export einen
  CalDAV-Server eintragen (Nextcloud, Radicale, iCloud, …); der Worker sendet dann Titel,
  Beschreibung, Fälligkeit, Priorität, Status und einen Link zur Mail dorthin. Der Nutzer sieht
  das vor dem Einschalten. Liegt der Server außerhalb des eigenen Hauses, ist das eine
  Übermittlung an einen Dritten. Mit `gtasks` verbindet der Nutzer sein Google-Konto per OAuth
  (Scope `tasks`, OAuth-Client aus `OLLAMAIL_GMAIL_*`); dieselben Felder ohne Priorität gehen dann
  an Google, also an einen Dritten (Drittland), siehe `docs/providers/gmail.md` §8.
  Die Instanz baut bei CalDAV Verbindungen zu vom Nutzer eingetragenen
  Adressen auf, auch im internen Netz (dafür ist der Export lokal gedacht); wer das nicht
  möchte, lässt den Export aus oder begrenzt ausgehende Verbindungen des Workers per Firewall.
- **Keine Telemetrie**, keine externen Fonts oder CDNs. Die Content-Security-Policy der UI erlaubt
  nur den eigenen Origin.

### 9.3 Logs

- Die API loggt strukturiert als JSON auf stdout. Ein PII-Filter verwirft u. a. Betreffzeilen,
  Adressen, Inhalte, Prompts und Tokens; Zugriffslogs enthalten nur Routen-Templates
  (Details: [`PRIVACY.md`](PRIVACY.md#logging-im-detail)).
- Logs landen im Docker-Logging des Hosts (`docker compose -f deploy/compose.yaml logs`). Der
  Docker-Standardtreiber `json-file` **rotiert nicht**: Rotation in `/etc/docker/daemon.json`
  einrichten (`"log-opts": {"max-size": "10m", "max-file": "5"}`) und die Aufbewahrung in das
  Löschkonzept aufnehmen.
- Der Log-Level `DEBUG` (`OLLAMAIL_LOG_LEVEL`) ist nur zur Fehlersuche gedacht.

### 9.4 Hinweise für Verarbeitungsverzeichnis, DSFA und Betriebsrat

- **Admin ≠ Leser:** Admins verwalten die Instanz, sehen aber keine fremden Mail-Inhalte, nur
  Metadaten und aggregierte Statistiken (Konzept in [`PRIVACY.md`](PRIVACY.md); umgesetzt mit den
  Admin-Funktionen #18, #33, #35).
- **Technischer Zugriff:** Wer Root-Zugriff auf den Host oder die Datenbank hat, kann alle Daten
  lesen. Diesen Personenkreis klein halten und im Berechtigungskonzept festhalten. Für Daten im
  Ruhezustand ein verschlüsseltes Dateisystem bzw. Volume verwenden.
- **Betriebsrat / Mitarbeiterüberwachung:** ollamail bietet keine Funktionen zur Leistungs- oder
  Verhaltenskontrolle. Admin-Statistiken sind aggregiert und nicht personenbezogen auswertbar.
  Da die Software dennoch Daten von Beschäftigten verarbeitet, sollte der Betriebsrat vor der
  Einführung beteiligt werden (in Deutschland u. a. § 87 Abs. 1 Nr. 6 BetrVG).
- **Betroffene Dritte:** E-Mails enthalten personenbezogene Daten der Kommunikationspartner. Der
  Umfang lässt sich über Postfach- und Ordnerauswahl und den Erstimport-Zeitraum begrenzen
  (`OLLAMAIL_MAIL_INITIAL_SYNC_DAYS`, Standard 90 Tage).

## 10. Troubleshooting

Erste Schritte bei jedem Problem:

```sh
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail=100 <dienst>
curl http://localhost:8080/api/readyz
```

| Symptom | Ursache und Lösung |
|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` | `deploy/.env` fehlt oder `POSTGRES_PASSWORD` ist leer. Abschnitt 2.2. |
| `service "migrate" didn't complete successfully` | Ursache in `logs migrate`. Häufig: siehe nächste Zeile. |
| `password authentication failed for user "ollamail"` (in `logs migrate`/`api`) | `POSTGRES_PASSWORD` wurde nach dem ersten Start geändert. Altes Passwort wieder eintragen oder das Passwort in der Datenbank anpassen: `docker compose -f deploy/compose.yaml exec postgres psql -U ollamail -d ollamail -c "ALTER USER ollamail PASSWORD '<neu>'"` (Nutzer/DB-Name ggf. an `POSTGRES_USER`/`POSTGRES_DB` anpassen). |
| `/api/readyz` liefert `503` mit `"database":"failed"` | Datenbank nicht erreichbar. `logs postgres` und `OLLAMAIL_DATABASE_URL` prüfen (Sonderzeichen im Passwort URL-kodieren). |
| `migrate` zeigt `Exited (0)` | Normal: einmaliger Migrationslauf. |
| `error from registry: unauthorized` / `pull access denied` für `ghcr.io/dusseligerdussel/ollamail-*` | Kein Zugriff auf die GHCR-Images oder es gibt noch kein Release für `OLLAMAIL_VERSION` (`latest` erst ab dem ersten Release). Lokal bauen ([2.3](#23-starten), Weg A) oder `docker login ghcr.io` ([`deploy/README.md`](../deploy/README.md#zugriff-auf-die-images)). |
| `setup_pending` im Log ohne `setup_code` | `OLLAMAIL_SETUP_TOKEN` ist gesetzt; diesen Wert im Setup-Assistenten eingeben. |
| `worker` startet ständig neu | `docker compose -f deploy/compose.yaml logs worker`; häufig ein ungültiger Wert in `OLLAMAIL_WORKER_QUEUES`. |
| `toomanyrequests` / `429 Too Many Requests` beim Build oder Pull | Rate-Limit von Docker Hub. Mit `docker login` anmelden oder später erneut versuchen. |
| `failed to bind host port … address already in use` oder `port is already allocated` | Port 8080 ist belegt. `OLLAMAIL_HTTP_PORT` in `deploy/.env` ändern. |
| UI lädt hinter dem Reverse Proxy, Live-Updates bleiben aus | Pufferung im äußeren Proxy aktiv. Abschnitt 4 (SSE). |
| `could not select device driver "nvidia"` | NVIDIA Container Toolkit fehlt oder Docker wurde danach nicht neu gestartet. Abschnitt 3.3. |
| Container meldet `Read-only file system` | Gewollt: Root-Dateisystem ist schreibgeschützt, beschreibbar sind nur `/tmp` und `/data`. |

Für ausführlichere Logs vorübergehend `OLLAMAIL_LOG_LEVEL=DEBUG` setzen und
`docker compose -f deploy/compose.yaml up -d` ausführen. Auch dann enthalten die Logs keine
Mail-Inhalte.
