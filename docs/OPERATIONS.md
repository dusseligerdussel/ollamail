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
| `POSTGRES_PASSWORD` | Datenbank-Passwort, z. B. `openssl rand -hex 24`. Hex vermeidet Sonderzeichen, die in `OLLAMAIL_DATABASE_URL` URL-kodiert werden müssten. In `.env.example` bewusst leer: Ohne Wert startet Compose nicht, den früheren Platzhalter `change-me` lehnen `api`, `worker` und `migrate` ab. |

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
| Worker | Kein HTTP-Endpunkt. Der Worker schreibt alle `OLLAMAIL_WORKER_HEARTBEAT_INTERVAL_SECONDS` (Standard 30) eine Heartbeat-Datei, solange Event-Loop und alle Job-Worker laufen; der Compose-Healthcheck (`python -m app.core.heartbeat`) meldet `unhealthy`, wenn sie älter als vier Intervalle ist. `docker compose ps` zeigt den Zustand. |

Die UI ist unter `http://localhost:8080` erreichbar – standardmäßig **nur auf dem Docker-Host
selbst** (`OLLAMAIL_HTTP_BIND=127.0.0.1`). Für den Zugriff aus dem Netz einen Reverse Proxy mit TLS
davorsetzen ([Abschnitt 4](#4-reverse-proxy-und-tls)) oder – bewusst, z. B. im Heimnetz –
`OLLAMAIL_HTTP_BIND` auf die LAN-Adresse des Hosts bzw. `0.0.0.0` setzen; dann spricht die UI
unverschlüsseltes HTTP mit jedem, der den Port erreicht. Identity-Provider (Entra ID, Google, OIDC,
LDAP/Active Directory), Rollen-Zuordnung und Nutzer verwaltet der Admin unter Admin → Anmeldung
bzw. Nutzer ([`auth/admin.md`](auth/admin.md)), ebenso GitHub ([`auth/github.md`](auth/github.md)) und SAML
([`auth/saml.md`](auth/saml.md)). Nutzer und Gruppen aus Entra ID oder Okta überträgt SCIM
(Admin → SCIM-Provisionierung, [`auth/scim.md`](auth/scim.md)).

**Erst-Admin:** Solange kein Nutzer existiert, leitet die UI auf den Setup-Assistenten (`/setup`),
der über `POST /api/setup` den ersten Admin anlegt und direkt anmeldet. Dafür
ist ein Setup-Token nötig – `OLLAMAIL_SETUP_TOKEN` oder, falls leer, ein aus `OLLAMAIL_SECRET_KEY`
abgeleiteter Wert. Die API schreibt ihn beim Start ins Log (Event `setup_pending`, Feld
`setup_code`), solange die Instanz nicht eingerichtet ist. Ist `OLLAMAIL_SETUP_TOKEN` gesetzt,
steht der Token nicht im Log; ein eigener Token braucht mindestens 32 Zeichen (z. B.
`openssl rand -hex 16`), sonst startet die API nicht. Setup-Versuche zählen wie Logins gegen das
Limit pro Client-IP (`OLLAMAIL_AUTH_IP_MAX_ATTEMPTS`). Eine der beiden Varianten genügt:

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
davorsetzen oder – nur für Testinstallationen – `OLLAMAIL_AUTH_COOKIE_SECURE=false`,
siehe [2.6](#26-http-ohne-tls-testbetrieb).

Für den Betrieb im Netz unbedingt TLS davorsetzen: [Abschnitt 4](#4-reverse-proxy-und-tls).

### 2.5 Stoppen

```sh
docker compose -f deploy/compose.yaml down      # Container stoppen, Daten bleiben erhalten
```

`down -v` löscht zusätzlich **alle Volumes inklusive Datenbank**. Nur verwenden, wenn die Daten
wirklich weg sollen.

### 2.6 HTTP ohne TLS (Testbetrieb)

Sitzungs- und CSRF-Cookie sind standardmäßig `Secure` (`OLLAMAIL_AUTH_COOKIE_SECURE=true`).
Über `http://<ip>:8080` oder einen anderen Hostnamen als `localhost` speichern Browser sie nicht.
Jede Anfrage, die etwas ändert, lehnt die API dann mit `403` und `error_code: "csrf_failed"` ab:
Setup und Anmeldung schlagen fehl. Setup- und Anmeldeseite zeigen in diesem Fall den Hinweis
„Unverschlüsselte Verbindung (HTTP)“ bzw. „Der Browser hat das Sicherheits-Cookie nicht gesendet“.

Lösung, in dieser Reihenfolge:

1. **HTTPS einrichten** – Reverse Proxy mit TLS vor ollamail, siehe [Abschnitt 4](#4-reverse-proxy-und-tls).
   Das ist auch im Heimnetz der empfohlene Weg.
2. **Nur zum Ausprobieren** im eigenen, vertrauenswürdigen Netz: in `deploy/.env`
   `OLLAMAIL_AUTH_COOKIE_SECURE=false` und – damit die UI aus dem LAN erreichbar ist –
   `OLLAMAIL_HTTP_BIND` auf die LAN-Adresse des Hosts setzen (siehe [2.4](#24-prüfen)), dann
   `docker compose -f deploy/compose.yaml up -d` ausführen.

Risiken von `OLLAMAIL_AUTH_COOKIE_SECURE=false`: Passwörter, Sitzungs-Cookies und alle Mail-Inhalte
gehen unverschlüsselt durchs Netz. Wer den Verkehr mitlesen kann (geteiltes WLAN, kompromittiertes
Gerät im Netz), kann Sitzungen übernehmen. Für den regulären Betrieb und für jede Erreichbarkeit
aus dem Internet nicht geeignet; nach dem Test wieder auf `true` setzen.

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
```

Die API lädt fehlende Modelle beim Start selbst aus Ollama (`OLLAMAIL_LLM_PULL_MISSING_MODELS=true`,
Standard in `deploy/.env.example`; sie wartet dafür etwa eine Minute auf den Ollama-Container).
Die Modelle landen im Volume `ollama-models`. Bis der Download fertig ist, zeigt die UI Admins
einen Hinweis „Modell fehlt“. Auf der Admin-Seite (`/admin`, Abschnitt „Sprachmodelle“) steht der
Zustand je Aufgabe; ein fehlendes Modell lässt sich dort auch per Button herunterladen (Job
`ai.pull_model` mit Fortschrittsanzeige). Von Hand geht es weiterhin so:

```sh
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull qwen2.5:3b
docker compose -f deploy/compose.yaml exec ollama-cpu ollama pull bge-m3
```

Mit `OLLAMAIL_LLM_READINESS_CHECK=true` meldet `/api/readyz` fehlende Modelle als `"llm":"failed"`.

Ollama ist nur für `api` und `worker` im internen Compose-Netz `backend` unter
`http://ollama:11434` erreichbar, der Port wird nicht veröffentlicht; `frontend` erreicht Ollama
nicht. Für Modell-Downloads hängt Ollama zusätzlich am Netz `egress`; wie man das nach dem Laden
der Modelle abschaltet, steht in [`deploy/README.md`](../deploy/README.md#netze). Modelle liegen
im Volume `ollama-models`.

Das Profil muss bei `up` und `down` mit angegeben werden, sonst wird der Dienst nicht gestartet
bzw. nicht gestoppt (`exec` und `logs` auf einen laufenden Dienst gehen auch ohne). Alternativ
`COMPOSE_PROFILES=ollama-cpu` in der Shell oder in `deploy/.env` setzen.

#### Erstimport auf CPU: Dauer, Altbestand, Zeitscheiben (#141)

Auf CPU mit `qwen2.5:3b` brauchen Triage und Aufgaben zusammen etwa **20–60 s pro Mail**
(Messung auf 4 vCPUs, [`operations/model-evals.md`](operations/model-evals.md), Abschnitt 4.1:
Triage im Mittel 9,5 s, Aufgaben 46,3 s, Median 14,1 s). Seit #158 (Abschnitt 4.5: Triage 8,2 s,
Aufgaben 8,3 s, keine Timeouts) sind es im Mittel rund 16 s; die Angaben unten bleiben als obere
Abschätzung stehen. Würde jede importierte Mail
klassifiziert, dauerte der Standard-Import (90 Tage, oft 5.000–10.000 Mails) **1–4 Tage**. Der
Suchindex ist dagegen billig: Embeddings für 200 Mails brauchten 35 s, für 10.000 Mails also
rund eine halbe Stunde. Deshalb gilt:

- **Nur jüngere Mails werden klassifiziert.** Mails, die vor mehr als
  `OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS` Tagen (Standard **14**) eingegangen sind, bekommen nur
  Volltextindex und Embeddings – Suche und „Frag dein Postfach“ funktionieren –, aber keine Triage
  und keine Aufgaben (Schritte im Status `skipped`). Auch Aufgaben aus monatealten Mails entfallen
  so. Bei 14 Tagen sind das typischerweise einige hundert bis gut tausend Mails, also grob
  **3–17 Stunden** LLM-Zeit statt Tagen; neue Mails überholen den Rückstand trotzdem sofort.
  `0` klassifiziert alle Mails.
- **Ältere Mails nachträglich klassifizieren:** Admin › Systemstatus, Abschnitt „Verarbeitung je
  Postfach“, Button „Ältere Mails auch klassifizieren“, oder
  `python -m app.cli processing include-older <mailbox-id>`. Die übersprungenen Mails laufen dann
  hinter neuen Mails durch; für dieses Postfach gilt die Grenze danach nicht mehr (auch nicht für
  später importierte Mails).
- **Der Import läuft in Zeitscheiben.** Ein Sync-Job beendet den Import nach
  `OLLAMAIL_MAIL_SYNC_SLICE_BATCHES` Batches (Standard 20, à `OLLAMAIL_MAIL_SYNC_BATCH_SIZE` = 50
  Mails) oder `OLLAMAIL_MAIL_SYNC_SLICE_MINUTES` Minuten (Standard 5) und plant sich mit
  niedrigerer Priorität neu ein. Der nächste Lauf holt zuerst neue Mails und Änderungen, dann geht
  der Import weiter. Neue Mails warten so höchstens eine Zeitscheibe statt bis zum Ende des
  Imports. Ausnahme Microsoft 365: Die Delta-Abfrage eines Ordners liefert neue Mails erst, wenn
  dessen Erstimport durch ist; andere Ordner sind davon nicht betroffen. `0` schaltet die jeweilige
  Grenze ab.

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
- Wer die Modelle dieses Servers selbst verwaltet, setzt `OLLAMAIL_LLM_PULL_MISSING_MODELS=false`;
  dann lädt nur noch der Button auf der Admin-Seite (`/admin`) auf ausdrücklichen Wunsch.

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

**Welche Stimmen Nutzer wählen können (#191):** die Standardstimmen, alle bereits installierten
Stimmen und die in `OLLAMAIL_TTS_VOICE_ALLOWLIST` (kommagetrennte Voice-IDs, nur DE/EN) freigegebenen.
Heruntergeladen werden ausschließlich Standard- und Allowlist-Stimmen – vom Worker, nie auf
Wunsch eines Nutzers; eine andere Stimme lehnt die API ab (422), so kann niemand über die
Stimmenwahl das Daten-Volume füllen. Eine Stimme ohne Internetzugang bereitstellen: Dateien wie
unten ins Volume kopieren, sie ist dann installiert und wählbar.

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
   Das löscht alle Vektoren, ändert die Spalte auf `halfvec(<n>)` und legt den HNSW-Index neu an
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
- `Host`, `X-Forwarded-For` und `X-Forwarded-Proto` setzen. `X-Forwarded-For` muss die **echte
  Client-IP setzen**, nicht an einen vom Client mitgeschickten Wert anhängen – sonst kann ein
  Angreifer mit wechselnden Fantasiewerten die Rate-Limits (Login, Registrierung, SCIM) umgehen.
  Die Beispiele unten tun das.
- Der interne Caddy übernimmt `X-Forwarded-*` nur von privaten Netzen (RFC 1918, Loopback); ein
  Proxy auf demselben Host oder im selben LAN erfüllt das. Als Client-IP gilt der rechteste
  Eintrag in `X-Forwarded-For`, der nicht aus einem privaten Netz stammt (`trusted_proxies_strict`);
  an die API gibt Caddy genau diesen einen Wert weiter. Die API wertet `X-Forwarded-*` nur von
  `OLLAMAIL_FORWARDED_ALLOW_IPS` aus (Standard: Loopback und private Netze, also der Caddy im
  Compose-Netz).
- Wer ohne vorgeschalteten Proxy direkt aus einem privaten Netz (LAN, VPN) zugreift, gilt für Caddy
  selbst als vertrauenswürdiger Proxy und kann seine IP per `X-Forwarded-For` frei wählen. Das
  betrifft nur Clients im internen Netz; aus dem Internet ist der Header wirkungslos.
- **Server-Sent Events** (Live-Updates, gestreamte Antworten von „Frag deine Inbox“):
  Antwort-Pufferung abschalten und lange Verbindungen erlauben. Der interne Caddy nutzt dafür
  bereits `flush_interval -1` und 1 h Timeout – der äußere Proxy muss mindestens genauso großzügig
  sein.
- **Kompression:** Der interne Caddy komprimiert JSON-Antworten der API (`zstd`, `gzip`) und die
  statischen Dateien, Server-Sent Events (`text/event-stream`) bewusst nicht: komprimierte
  Streams würden gepuffert. Der äußere Proxy muss nichts komprimieren; wer es dort einschaltet,
  nimmt `text/event-stream` aus (nginx: nicht in `gzip_types` aufnehmen; Caddy: `encode` mit
  `match` auf `application/json*`, siehe `frontend/caddy/Caddyfile`).

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

Caddy setzt `X-Forwarded-*` automatisch: Ohne `trusted_proxies` verwirft es einen vom Client
mitgeschickten `X-Forwarded-For` und setzt die echte Client-IP.

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
        # Echte Client-IP setzen, nicht anhängen ($proxy_add_x_forwarded_for übernähme
        # einen vom Client gefälschten Wert)
        proxy_set_header X-Forwarded-For $remote_addr;
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

Traefik verwirft `X-Forwarded-*` von Clients, die nicht in `forwardedHeaders.trustedIPs` des
Entrypoints stehen, und setzt die echte Client-IP. Deshalb am Entrypoint **kein**
`forwardedHeaders.insecure: true` setzen und `trustedIPs` nur mit den Adressen eines weiteren,
vorgeschalteten Proxys (z. B. Load Balancer) füllen.

Läuft Traefik selbst als Container, ist `127.0.0.1` dort der Traefik-Container. Dann die
IP-Adresse des Docker-Hosts eintragen und `OLLAMAIL_HTTP_BIND` auf diese Adresse setzen – oder
Traefik an das Netz `ollamail_edge` hängen und `http://frontend:8080` eintragen (nie
`api:8000` direkt, sonst fehlen Header, Metrik-Sperre und Client-IP-Ermittlung des internen Caddy;
siehe [Netze](../deploy/README.md#netze)).

### 4.4 Sicherheits-Header

Der interne Caddy setzt bereits CSP, `X-Frame-Options`, `Referrer-Policy` u. a.
(Details: [`deploy/README.md`](../deploy/README.md#sicherheits-header)). Der äußere Proxy sollte diese
nicht überschreiben.

**HSTS (`Strict-Transport-Security`) muss der äußere Proxy setzen.** ollamail terminiert TLS nicht
selbst und sendet den Header deshalb standardmäßig nicht. Ohne HSTS kann ein Angreifer im Netz den
ersten Aufruf auf HTTP herabstufen und Sitzungs-Cookies abgreifen. Die Beispiele aus 4.1–4.3, ergänzt:

```caddyfile
mail.example.org {
	header Strict-Transport-Security "max-age=31536000; includeSubDomains"
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
```

```nginx
# im server-Block mit listen 443; "always" auch für Fehlerantworten
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

```yaml
# Traefik (File-Provider): Middleware definieren und am Router eintragen
http:
  middlewares:
    hsts:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
  routers:
    ollamail:
      middlewares: [hsts]
```

`includeSubDomains` nur, wenn alle Subdomains per HTTPS erreichbar sind; mit kurzem `max-age`
(z. B. 300) beginnen und erst nach einem Test erhöhen. Kann der äußere Proxy keine Header setzen,
sendet der `frontend`-Container den Header selbst: `OLLAMAIL_HSTS=max-age=31536000; includeSubDomains`
in `deploy/.env`, danach `docker compose -f deploy/compose.yaml up -d`. Nur bei Zugriff
ausschließlich über HTTPS setzen; im Testbetrieb über HTTP (2.6) leer lassen.

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

PostgreSQL (`POSTGRES_IMAGE`, Standard `pgvector/pgvector:0.8.7-pg16-bookworm`) und Ollama
(`OLLAMA_IMAGE`, Standard `ollama/ollama:0.35.0`) sind auf feste Versionen gepinnt und über
Variablen in `deploy/.env` änderbar. Neue Versionen kommen mit einem ollamail-Update (Dependabot
schlägt sie im Repository vor); wer `POSTGRES_IMAGE` oder `OLLAMA_IMAGE` selbst setzt, pflegt die
Version selbst. Beim PostgreSQL-Image Hauptversion **und** Debian-Release (`bookworm`) beibehalten:
ein anderes Debian-Release bringt eine andere glibc und damit andere Sortierregeln
(Collations); PostgreSQL warnt dann bei jeder Verbindung, Indizes auf Textspalten müssen mit
`REINDEX DATABASE` neu aufgebaut werden. Bisher lief der gleitende Tag `pg16`, der derzeit auf
`bookworm` zeigt – der Wechsel auf den gepinnten Tag ist für bestehende Installationen daher ohne
Weiteres möglich. Ein Wechsel der **PostgreSQL-Hauptversion** (z. B. 16 → 17)
ist mit dem bestehenden Volume nicht möglich: Backup ziehen, neue Version mit leerem Volume
starten, Backup einspielen (Abschnitt 5).

### 6.6 Upgrade-Hinweise: sichere Standardwerte (#143)

Seit diesem Stand sind einige Standardwerte strenger. Bestehende Installationen prüfen vor dem
Update:

1. **UI nur noch lokal veröffentlicht.** `compose.yaml` bindet Port 8080 ohne Angabe an
   `127.0.0.1` statt an alle Schnittstellen. Steht in der eigenen `deploy/.env` bereits
   `OLLAMAIL_HTTP_BIND=…`, ändert sich nichts. Wer die UI bisher direkt aus dem LAN aufgerufen hat
   (ohne Reverse Proxy auf demselben Host), setzt den Zugriff bewusst wieder:

   ```sh
   OLLAMAIL_HTTP_BIND=0.0.0.0        # alle Schnittstellen, oder die LAN-Adresse des Hosts
   ```

   Ein Reverse Proxy auf demselben Host funktioniert mit dem neuen Standard unverändert.
2. **Datenbank-Passwort `change-me` wird abgelehnt.** `api`, `worker` und `migrate` starten nicht,
   wenn `OLLAMAIL_DATABASE_URL` noch den Platzhalter enthält (Log: `uses the placeholder password`).
   Das Passwort in der Datenbank **und** in `deploy/.env` ändern – `POSTGRES_PASSWORD` wirkt nur
   beim ersten Start:

   ```sh
   new="$(openssl rand -hex 24)"
   docker compose -f deploy/compose.yaml exec postgres \
     psql -U ollamail -d ollamail -c "ALTER USER ollamail PASSWORD '$new'"
   sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$new|" deploy/.env
   docker compose -f deploy/compose.yaml up -d
   ```

   (Nutzer- und Datenbankname ggf. an `POSTGRES_USER`/`POSTGRES_DB` anpassen.)
3. **Eigener Setup-Token mindestens 32 Zeichen.** Ein kürzeres `OLLAMAIL_SETUP_TOKEN` stoppt den
   Start. Ist die Instanz eingerichtet, wird der Token nicht mehr gebraucht: Variable leeren.
4. **Postfächer auf internen Adressen.** IMAP- und SMTP-Server müssen auf eine öffentliche Adresse
   auflösen. Loopback, private Netze (RFC 1918), Link-Local, ULA usw. lehnt ollamail ab – mit
   demselben Fehler wie bei einem nicht erreichbaren Server (`connection_failed`), damit Nutzer
   darüber nicht das interne Netz abtasten können. Läuft der eigene Mailserver im LAN, auf dem
   Docker-Host oder im Cluster, ihn bewusst erlauben:

   ```sh
   OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS=imap.lan,192.168.10.0/24
   ```

   Hostnamen gelten für jede Adresse, auf die sie auflösen; IP-Adressen und CIDR-Bereiche für
   genau diese Ziele. Betroffene Postfächer zeigen bis dahin den Sync-Fehler `connection_failed`
   und laufen nach dem Neustart von `api` und `worker` ohne weiteres Zutun weiter.
5. **Verbindungstests begrenzt.** Pro Nutzer sind 20 Verbindungstests in 10 Minuten möglich
   (Testen, Anlegen, Verbindung ändern; `OLLAMAIL_MAIL_CONNECTION_TEST_MAX_ATTEMPTS`).
6. **CalDAV-Export auf internen Adressen (#189).** Dieselbe Prüfung gilt für CalDAV-Server des
   Aufgaben-Exports (`OLLAMAIL_TODOS_EXPORT_SINKS=caldav`). Ein Nextcloud oder Radicale im LAN,
   auf dem Docker-Host oder im Cluster muss erlaubt werden:

   ```sh
   OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS=nextcloud.lan,192.168.10.0/24
   ```

   Bis dahin meldet der Export `unavailable` (Log `todo_export_destination_refused`) und läuft
   nach dem Neustart von `api` und `worker` weiter. Verbindungen zum CalDAV-Server nutzen keine
   Proxy-Variablen (`HTTPS_PROXY`) mehr, sondern gehen direkt zur geprüften Adresse.

### 6.7 Upgrade-Hinweis: Embeddings als `halfvec` (#164)

Die Migration `5dab8560b40a` (`store embeddings as halfvec`) stellt die Spalte
`search_embeddings.embedding` von `vector(n)` (32 Bit je Dimension) auf `halfvec(n)` (16 Bit) um.
Tabelle und HNSW-Index werden dadurch etwa halb so groß, der Index bei 1024 Dimensionen sogar
rund ein Drittel (mit `vector` passt nur ein Vektor auf eine 8-KB-Seite). Die vorhandenen Vektoren
werden umgerechnet, nicht neu berechnet: Das LLM wird dafür nicht gebraucht.

**Was passiert:** HNSW-Index löschen, Spalte per `ALTER TABLE … TYPE halfvec(n) USING
embedding::halfvec(n)` umschreiben (`n` ist die aktuelle Länge der Spalte, auch nach
`search resize`), HNSW-Index mit `halfvec_cosine_ops` neu aufbauen. Alles in der Transaktion
des Dienstes `migrate`; schlägt ein Schritt fehl, bleibt die Datenbank auf dem alten Stand.

**Voraussetzung:** pgvector **0.7 oder neuer**. Das mitgelieferte Image `pgvector/pgvector:pg16`
und die CloudNativePG-Images „standard“ enthalten 0.8. Bei älteren Installationen bricht die
Migration mit einer Meldung ab; dann das PostgreSQL-Image aktualisieren und in der Datenbank
`ALTER EXTENSION vector UPDATE;` ausführen. Version prüfen:

```sh
docker compose -f deploy/compose.yaml exec postgres \
  psql -U ollamail -d ollamail -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
```

**Dauer und Sperre:** Während der Migration ist `search_embeddings` exklusiv gesperrt; `api` und
`worker` starten erst danach (Compose wartet auf `migrate`). Mail-Sync, Suche und „Frag deine
Inbox“ sind so lange nicht verfügbar. Die Dauer hängt vor allem vom Index-Aufbau und davon ab,
ob der HNSW-Graph in `maintenance_work_mem` passt ([8.4](#84-postgresql-tuning-pgvector)).
Gemessen mit 1024 Dimensionen, 4 vCPUs, Compose-Standardwerten (`maintenance_work_mem` 512 MB,
`shm_size` 1g):

| Embeddings (Abschnitte) | vorher Tabelle / Index | nachher Tabelle / Index | Upgrade | Downgrade |
|---|---|---|---|---|
| 100 000 | 537 MB / 781 MB | 275 MB / 260 MB | 68 s | 82 s |
| 300 000 | 1,6 GB / 2,3 GB | 823 MB / 781 MB | 8 min | 17 min |

Grobe Schätzung für 1 Mio. Abschnitte: mit `POSTGRES_MAINTENANCE_WORK_MEM=4GB` (Index passt in
den Speicher) etwa 15–20 Minuten, mit 512 MB eine Stunde oder länger. Wie viele Embeddings es
gibt, zeigt `python -m app.cli search status`. Für große Instanzen vor dem Update
`POSTGRES_MAINTENANCE_WORK_MEM` und `POSTGRES_SHM_SIZE` wie in 8.4 anheben und das Update in ein
Wartungsfenster legen. Während der Migration braucht die Datenbank kurzzeitig zusätzlichen
Plattenplatz für die neue Tabelle und den neuen Index (rund die Hälfte der bisherigen Größe).

**Suchqualität:** 16 Bit reichen für die Kosinus-Ähnlichkeit normierter Embeddings. Auf dem
Eval-Datensatz ([`operations/model-evals.md`](operations/model-evals.md)) lieferte `halfvec`
für alle 48 beantwortbaren Fragen exakt dieselben Treffer wie `vector` (Hybrid-Suche, exakte
Vektorsuche und HNSW).

**Zurück:** Die Migration ist umkehrbar. Vor dem Wechsel auf eine ältere Version mit dem
**neuen** Image `alembic downgrade 5c672b257a5b` ausführen
(`docker compose -f deploy/compose.yaml run --rm --no-deps migrate alembic downgrade 5c672b257a5b`);
das wandelt die Spalte zurück in `vector(n)` und baut den Index neu (Dauer siehe Tabelle). Die
Werte behalten dabei die 16-Bit-Genauigkeit. Der sichere Weg bleibt das Backup ([6.4](#64-rollback-auf-die-vorherige-version)).

### 6.8 Upgrade-Hinweis: getrennte Netze und gepinnte Images (#192)

- **Compose-Netze.** Statt eines gemeinsamen Netzes gibt es `edge`, `backend` (intern) und
  `egress` ([`deploy/README.md`](../deploy/README.md#netze)). `docker compose -f deploy/compose.yaml up -d`
  legt sie an; Daten und Volumes bleiben unverändert. Das alte Netz bleibt übrig:
  `docker network rm ollamail_default`. Eigene Container, die an `ollamail_default` hingen
  (Prometheus, Reverse Proxy, externer Ollama-Container), an `ollamail_backend` bzw.
  `ollamail_edge` hängen. Wer mit `compose.dev.yaml` arbeitet, braucht nichts zu tun.
- **Ollama gehärtet.** Schreibgeschütztes Dateisystem, keine Capabilities, `no-new-privileges`.
  Bestehende Modelle im Volume `ollama-models` bleiben nutzbar.
- **PostgreSQL-Image gepinnt** auf `pgvector/pgvector:0.8.7-pg16-bookworm` – derzeit derselbe
  Stand wie der bisherige Tag `pg16` (PostgreSQL 16, Debian bookworm); ein älteres, lokal
  gezogenes `pg16` bringt höchstens ein älteres pgvector mit. Die Datenbank bleibt unverändert
  ([6.5](#65-drittanbieter-images)).
- **HSTS** im äußeren Proxy setzen, falls noch nicht geschehen ([4.4](#44-sicherheits-header)).
- **Helm:** `networkPolicy.enabled` ist jetzt standardmäßig `true`
  ([`kubernetes.md` §11](operations/kubernetes.md#11-sicherheit-und-networkpolicies)).

## 7. Schlüsselverwaltung

`OLLAMAIL_SECRET_KEY` ist der Master-Key für die Verschlüsselung gespeicherter Zugangsdaten
(Postfach-Passwörter, OAuth-Tokens, IdP-Secrets; Envelope-Encryption mit AES-256-GCM, Verfahren
siehe [`PRIVACY.md`](PRIVACY.md)). Ohne gültigen Key startet `api` nicht.

- Geht der Key verloren, sind gespeicherte Zugangsdaten nicht mehr lesbar. Postfächer und
  Identity-Provider müssen dann neu eingerichtet werden.
- Den Key nach der Einrichtung nicht einfach in `.env` austauschen, sondern rotieren.

**Key-Rotation:** Ablauf mit `OLLAMAIL_SECRET_KEYS_OLD` und `python -m app.cli rotate-keys` in
[`deploy/README.md`](../deploy/README.md#master-key-und-key-rotation).

**VAPID-Schlüssel für Web Push (#181):** Nur nötig, wenn Benachrichtigungen auch ohne offenen
Tab ankommen sollen. Web Push läuft immer über den Push-Dienst des Browser-Herstellers (Google,
Mozilla, Apple, Microsoft) und ist deshalb standardmäßig aus (siehe [`PRIVACY.md`](PRIVACY.md)).
Einschalten:

```bash
docker compose -f deploy/compose.yaml run --rm api python -m app.cli notifications vapid-keys
```

Die beiden ausgegebenen Zeilen in `.env` übernehmen, dazu
`OLLAMAIL_NOTIFICATIONS_VAPID_SUBJECT=mailto:<Kontaktadresse>` und
`OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ENABLED=true`; `api` und `worker` neu starten. Der private
Schlüssel gehört wie `OLLAMAIL_SECRET_KEY` nicht ins Repository (Helm: ins Secret). Ein neues
Schlüsselpaar macht alle eingerichteten Geräte ungültig; Nutzer schalten Web Push dann je Gerät
neu ein. Der Worker braucht ausgehend HTTPS zu den Push-Diensten
(`OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ALLOWED_HOSTS`).

## 8. Skalierung

Es läuft eine API-Instanz. Der `worker` (Procrastinate, Queue in PostgreSQL) übernimmt Mail-Sync,
KI-Verarbeitung, OCR und TTS und startet immer mit. Die Jobs sind nach Typ auf Queues verteilt
(`sync`, `llm`, `tts`, `ocr`, `default`, `push`); `OLLAMAIL_WORKER_QUEUES` legt fest, welche ein
Worker abarbeitet (`default` schließt `push` ein), sodass z. B. ein eigener Worker nur LLM-Jobs auf einem GPU-Host übernimmt. Mehr
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
auch unverschlüsselt) nur in Testumgebungen. Mailserver im eigenen Netz (private oder
Loopback-Adressen) müssen in `OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS` stehen, sonst lehnt ollamail die
Verbindung ab ([6.6](#66-upgrade-hinweise-sichere-standardwerte-143)).
IMAP-Server ohne CONDSTORE (z. B. manche Hoster) melden nicht, welche Flags sich geändert haben;
ollamail prüft dann je Sync nur die neuesten `OLLAMAIL_MAIL_IMAP_FLAG_WINDOW` Mails (Standard
1000) und alle Mails einmal in `OLLAMAIL_MAIL_IMAP_FULL_FLAG_SCAN_HOURS` (Standard 24).
Gelöschte und verschobene Mails werden trotzdem bei jedem Sync erkannt.

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

### 8.1 Ausfälle: hängende Jobs, LLM nicht erreichbar, fehlgeschlagene Schritte

Der Worker erholt sich selbst von den häufigsten Störungen; manuelles SQL ist nicht nötig.

- **Hängende Jobs.** Wird ein Worker mitten in einem Job beendet (OOM, `SIGKILL` nach der
  Grace-Period), bliebe der Job für immer „läuft“ – und mit ihm der Lock, etwa der des Postfachs,
  der jeden weiteren Sync blockiert. Der periodische Job `worker.retry_stalled_jobs` (alle
  5 Minuten) reiht Jobs erneut ein, deren Worker seit `OLLAMAIL_WORKER_STALLED_AFTER_SECONDS`
  (Standard 120) keinen Heartbeat mehr gesendet hat (Worker senden alle 10 s einen). Im Log:
  `worker_stalled_job_retried` mit Job-ID, Task-Name und Queue.
- **LLM nicht erreichbar.** Scheitern `OLLAMAIL_PROCESSING_LLM_BREAKER_THRESHOLD` (Standard 3)
  Aufrufe in Folge, weil der Endpunkt nicht antwortet (Verbindungsfehler, HTTP 5xx/429), pausiert
  der Worker diesen Endpunkt für `OLLAMAIL_PROCESSING_LLM_BREAKER_COOLDOWN_SECONDS` (Standard 30 s;
  bleibt er unten, jeweils doppelt so lange, höchstens das 16-Fache). Danach testet genau ein
  Aufruf, ob er wieder da ist. Verarbeitungsschritte warten in dieser Zeit, ohne Versuche zu
  verbrauchen; es entsteht kein Retry-Sturm gegen den toten Endpunkt. Im Log: `llm_circuit_open`
  und `llm_circuit_closed`, `processing_step_postponed`. Jeder Worker-Prozess entscheidet für sich.
- **Fehlgeschlagene Schritte.** Ein Schritt, dessen Versuche aufgebraucht sind, steht auf `failed`.
  Ist der Grund vorübergehend – LLM nicht erreichbar (`llm_unavailable_error`), Modell fehlt
  (`model_not_available_error`, wird nicht sofort wiederholt) –, plant der periodische Job
  `processing.retry_failed` (alle 5 Minuten) ihn automatisch neu ein: nach
  `OLLAMAIL_PROCESSING_AUTO_RETRY_DELAY_MINUTES` (Standard 15), danach jeweils doppelt so lange
  (höchstens ein Tag), insgesamt `OLLAMAIL_PROCESSING_AUTO_RETRY_ATTEMPTS`-mal (Standard 6, deckt
  rund 16 Stunden ab). Zeitüberschreitungen (`llm_timeout_error`) nur
  `OLLAMAIL_PROCESSING_AUTO_RETRY_TIMEOUT_ATTEMPTS`-mal (Standard 1). Dauerhafte Fehler (z. B.
  ungültige Modellausgabe nach dem Korrektur-Retry, `llm_output_error`/`llm_output_invalid`)
  bleiben fehlgeschlagen. Ein fehlendes Modell also einfach nachladen (`ollama pull …`); die
  betroffenen Mails laufen danach von selbst durch.
- **Postfach entfernen.** Das Entfernen läuft als Job `mail.delete_mailbox` im Hintergrund
  (Batches zu 500 Mails, gemessen rund 6 s je 100k Mails ohne Embeddings); bis dahin zeigt die
  Postfachliste „Wird gelöscht“, die Daten sind schon für niemanden mehr sichtbar. Im Log:
  `mail_mailbox_deletion_requested` und am Ende `mail_mailbox_deleted`. Bricht der Job ab, setzt
  `mail.resume_deletions` (alle 15 Minuten) fort.
- **Von Hand neu verarbeiten:** `python -m app.cli processing reprocess [--mailbox ID]` setzt
  auch dauerhafte Fehler zurück.

Für die Statusanzeige zählt `app.processing.service.count_steps_by_mailbox` ausstehende,
laufende und fehlgeschlagene Schritte (davon: automatische Wiederholung geplant) je Postfach;
`reset_failed_steps` setzt die fehlgeschlagenen eines Postfachs zurück.

### 8.2 Datenbankverbindungen

Jeder Prozess hat **einen** SQLAlchemy-Pool (`app.core.db.process_database`): Alle Jobs eines
Workers, der Postfach-Watcher und die KI-Einstellungen teilen ihn. Dazu kommen der Pool der
Job-Queue (Procrastinate) und einzelne dauerhafte `LISTEN`-Verbindungen. Mit
P = `OLLAMAIL_DATABASE_POOL_SIZE` (Standard 5) und O = `OLLAMAIL_DATABASE_MAX_OVERFLOW`
(Standard 10) braucht höchstens:

| Prozess | Verbindungen | Standard |
|---|---|---|
| `api` | P + O + 4 (Job-Queue, erst beim ersten eingereihten Job) + 1 (Live-Events) + 1 (KI-Einstellungen) | 21 |
| `worker` | P + O + Σ (Slots je Gruppe + 2) + 1 (Job-Queue) + 1 (KI-Einstellungen) + 1 (Postfach-Watcher, nur mit Queue `sync`) | 37 |
| `migrate`, CLI-Befehle | 1–2, nur kurz | – |

Slots je Gruppe (`app/worker.py`): `OLLAMAIL_WORKER_CONCURRENCY` für `sync`, `tts` und `default`
zusammen (Standard 4), max(`OLLAMAIL_LLM_CONCURRENCY`, `OLLAMAIL_LLM_MAX_CONCURRENCY`) für `llm`
(Standard 4), `OLLAMAIL_SEARCH_OCR_CONCURRENCY` für `ocr` (Standard 1) und
`OLLAMAIL_NOTIFICATIONS_WEB_PUSH_CONCURRENCY` für `push` (Standard 2). Ein Worker mit allen
Queues hat also (4 + 2) + (4 + 2) + (1 + 2) + (2 + 2) + 1 = 20 Verbindungen für die Job-Queue.

`max_connections` von PostgreSQL muss die Summe über alle Prozesse plus Reserve abdecken:

```
max_connections ≥ api × 21 + worker × 37 + 10 (migrate, CLI, psql, Backups)
                  + superuser_reserved_connections (Standard 3)
```

Beispiel: `--scale worker=2` braucht 21 + 2 × 37 + 13 = 108 – mehr als der
PostgreSQL-Standard (100). Die mitgelieferte Datenbank startet deshalb mit `max_connections=200`
(`POSTGRES_MAX_CONNECTIONS`, Abschnitt 8.4). Wer weiter skaliert, erhöht den Wert (jede
Verbindung kostet einige MB RAM) oder senkt P und O: Ein Worker braucht selten mehr gleichzeitige
SQLAlchemy-Verbindungen als Slots, P + O ≥ Summe der Slots reicht. Bei sehr vielen Replikaten
(Kubernetes) hilft ein Pooler im Modus `session` (`LISTEN/NOTIFY` und Advisory-Locks
funktionieren im Modus `transaction` nicht).

### 8.3 Monitoring (Prometheus)

Mit `OLLAMAIL_METRICS_ENABLED=true` liefern API und Worker Metriken im Prometheus-Format:

| Endpunkt | Inhalt |
|---|---|
| `http://api:8000/metrics` | Prozess-Metriken der API und die Datenbank-Metriken (Queue, Schritte, Sync) |
| `http://<worker>:9464/metrics` (`OLLAMAIL_METRICS_WORKER_PORT`) | Prozess-Metriken des Workers, v. a. die LLM-Aufrufe |

- **Nur intern.** Das Frontend leitet `/api/metrics` nicht weiter (`404`); erreichbar sind die
  Endpunkte nur im Compose-Netz bzw. Cluster. In Compose wird kein Port veröffentlicht: Prometheus
  im Docker-Netz `ollamail_backend` scrapt `api:8000` und die Worker-Container
  ([Netze](../deploy/README.md#netze)). Mit
  `OLLAMAIL_METRICS_TOKEN` verlangen beide Endpunkte zusätzlich
  `Authorization: Bearer <token>` (Prometheus: `authorization: {credentials: …}`).
- **Keine Inhalte.** Labels enthalten nur IDs, Codes und Konfigurationswerte: Postfach-ID (nie
  Adresse oder Anzeigename), Queue, Task-Name, Schritt, Fehlercode, Endpunkt-Name, Modell.
- Die Datenbank-Metriken liest die API höchstens alle `OLLAMAIL_METRICS_DATABASE_REFRESH_SECONDS`
  (Standard 60); die Abfragen zählen über Job- und Schritt-Tabelle.

| Metrik | Labels | Bedeutung |
|---|---|---|
| `ollamail_queue_jobs` | `queue`, `priority`, `status` (`todo`, `doing`) | Queue-Tiefe; Priorität 10 = neue Mail, 0 = Erstimport, −10 = Neuverarbeitung |
| `ollamail_queue_failed_jobs` | `queue`, `task` | fehlgeschlagene Jobs der letzten 7 Tage |
| `ollamail_processing_steps` | `step`, `status` (`pending`, `running`, `failed`) | Verarbeitungsschritte je Schritt |
| `ollamail_mailbox_processing_steps` | `mailbox_id`, `status` (+ `retry_scheduled`) | dasselbe je Postfach (wie Admin → System) |
| `ollamail_mailbox_sync_phase` | `mailbox_id`, `phase` | 1 für die aktuelle Sync-Phase (`error`, `idle`, `importing`, …) |
| `ollamail_mailbox_sync_error` | `mailbox_id`, `code` | 1, wenn der letzte Sync fehlschlug |
| `ollamail_mailbox_sync_failed_folders` | `mailbox_id` | Ordner mit fehlgeschlagenem Sync |
| `ollamail_mailbox_last_sync_timestamp_seconds` | `mailbox_id` | Zeitpunkt des letzten vollständigen Syncs |
| `ollamail_metrics_database_up` | – | 0, wenn die Datenbank-Metriken nicht lesbar waren |
| `ollamail_llm_request_duration_seconds` | `task`, `operation`, `endpoint`, `provider`, `model`, `outcome` | Dauer der LLM-Aufrufe (Histogramm) |
| `ollamail_llm_tokens_total` | dieselben, `kind` (`prompt`, `completion`) | gemeldete Tokens |
| `ollamail_llm_completion_tokens_per_second` | `endpoint`, `provider`, `model` | Tokens pro Sekunde erfolgreicher Aufrufe (Histogramm) |
| `ollamail_llm_errors_total` | dieselben wie Dauer ohne `outcome`, `error_type` | fehlgeschlagene Aufrufe |

Beispiel-Abfragen: Queue-Tiefe `sum by (queue) (ollamail_queue_jobs{status="todo"})`,
Tokens/s `rate(ollamail_llm_tokens_total{kind="completion"}[5m]) / rate(ollamail_llm_request_duration_seconds_sum[5m])`,
Postfächer mit Sync-Fehler `ollamail_mailbox_sync_error == 1`.

### 8.4 PostgreSQL-Tuning (pgvector)

Mit den PostgreSQL-Standardwerten (`shared_buffers` 128 MB, `maintenance_work_mem` 64 MB) dauert
der Aufbau des HNSW-Index für die Embeddings sehr lange, sobald er nicht mehr in
`maintenance_work_mem` passt. Die mitgelieferte Datenbank startet deshalb mit eigenen Werten
(`command: -c …` in `deploy/compose.yaml`), änderbar in `deploy/.env`:

| Variable | Standard | Hinweis |
|---|---|---|
| `POSTGRES_MAX_CONNECTIONS` | 200 | Formel in 8.2 |
| `POSTGRES_SHARED_BUFFERS` | 512MB | ca. 25 % des RAM, den PostgreSQL nutzen darf |
| `POSTGRES_EFFECTIVE_CACHE_SIZE` | 2GB | ca. 50–75 % des RAM; nur eine Planungsgröße |
| `POSTGRES_MAINTENANCE_WORK_MEM` | 512MB | Index-Aufbau; möglichst so groß wie der HNSW-Index |
| `POSTGRES_WORK_MEM` | 16MB | je Sortierung/Hash und Verbindung |
| `POSTGRES_SHM_SIZE` | 1g | `/dev/shm` des Containers; ≥ `maintenance_work_mem`, sonst scheitern parallele Index-Builds |

Die Standardwerte passen zu einem Host mit 8 GB RAM. Größenordnung für den Suchindex mit
bge-m3 (1024 Dimensionen, `halfvec` = 2 KB je Embedding, seit #164): 1 Mio. Abschnitte ≈ 2 GB
Tabelle und noch einmal etwa so viel HNSW-Index (mit `vector` waren es je rund 4 GB). Für solche Instanzen (16 GB RAM oder mehr)
`POSTGRES_SHARED_BUFFERS=4GB`, `POSTGRES_MAINTENANCE_WORK_MEM=4GB` und `POSTGRES_SHM_SIZE=5g`
setzen. Die Werte wirken nach `docker compose -f deploy/compose.yaml up -d postgres` (Neustart
der Datenbank); prüfen mit `SHOW shared_buffers;`. Für Kubernetes enthält
[`deploy/helm/examples/cnpg-cluster.yaml`](../deploy/helm/examples/cnpg-cluster.yaml) dieselben
Parameter; bei verwalteten Datenbanken setzt man sie in der Parametergruppe des Anbieters.

Die Embeddings sind als `halfvec` gespeichert (16 Bit je Dimension, ab pgvector 0.7), das
halbiert Tabelle und Index gegenüber `vector`. Messung und Upgrade-Hinweise:
[6.7](#67-upgrade-hinweis-embeddings-als-halfvec-164).

### 8.5 Ressourcen-Limits pro Nutzer

Damit ein einzelner Nutzer (oder ein gekapertes Konto) die Instanz nicht lahmlegen kann, begrenzt
die API einige Ressourcen pro Nutzer (#191). Die Zähler gelten **pro API-Prozess**: Mit mehreren
API-Replicas (Helm) kann ein Nutzer jedes Limit einmal je Pod ausschöpfen.

| Variable | Standard | Wirkung |
|---|---|---|
| `OLLAMAIL_LLM_API_USER_CONCURRENCY` | 2 | Gleichzeitige LLM-Anfragen eines Nutzers („Frag dein Postfach“, Antwortentwürfe, Suche). Weitere lehnt die API mit 429 und `Retry-After` ab (`error_code` `llm_busy`); die Oberfläche zeigt eine verständliche Meldung. Die Suche weicht stattdessen auf reine Volltextsuche aus. |
| `OLLAMAIL_LLM_API_CONCURRENCY` | 4 | Gleichzeitige LLM-Anfragen der API insgesamt; weitere warten auf einen freien Platz (wie die Jobs im Worker mit `OLLAMAIL_LLM_CONCURRENCY`). |
| `OLLAMAIL_EVENTS_MAX_STREAMS_PER_USER` | 10 | Offene Live-Update-Streams (`GET /api/events`, einer pro Browser-Tab). Weitere Verbindungen bekommen 429; der Tab funktioniert, aktualisiert sich aber nicht live. |
| `OLLAMAIL_EVENTS_SESSION_CHECK_INTERVAL` | 60 | Sekunden zwischen den Prüfungen, ob die Sitzung eines offenen Streams noch gilt. Nach Abmeldung, Widerruf der Sitzung, Ablauf oder Deaktivierung des Nutzers endet der Stream spätestens nach diesem Intervall (plus Heartbeat von 15 s), statt bis zum Proxy-Timeout weiterzulaufen. Die Prüfung verlängert die Sitzung nicht. |

Auf CPU-only-Hosts mit vielen Nutzern `OLLAMAIL_LLM_API_USER_CONCURRENCY=1` setzen; auf
GPU-Servern dürfen beide LLM-Werte höher sein. Welche TTS-Stimmen Nutzer wählen dürfen, steht in
[3.6](#36-sprachausgabe-tts).

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
| Modell-Downloads (Endpunkt, Modellname, Status, Bytes, Fehlercode; nicht personenbezogen) | PostgreSQL (`ai_model_pulls`) | vorhanden (#139) |
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
- Prometheus-Metriken (8.3) sind standardmäßig aus und enthalten nur IDs, Codes und Zähler.

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
| Setup meldet einen falschen Code oder die Anmeldung schlägt über `http://<ip>:8080` fehl, Hinweis „Unverschlüsselte Verbindung“; API antwortet `403` mit `csrf_failed` | Browser verwerfen die `Secure`-Cookies über HTTP. HTTPS einrichten oder nur zum Testen `OLLAMAIL_AUTH_COOKIE_SECURE=false`, siehe [2.6](#26-http-ohne-tls-testbetrieb). |
| `setup_pending` im Log ohne `setup_code` | `OLLAMAIL_SETUP_TOKEN` ist gesetzt; diesen Wert im Setup-Assistenten eingeben. |
| Sync eines Postfachs hängt nach einem Worker-Absturz | Löst sich nach spätestens 5 Minuten plus `OLLAMAIL_WORKER_STALLED_AFTER_SECONDS` von selbst (Abschnitt 8.1). Im Log `worker_stalled_job_retried`. |
| Viele Mails ohne Triage/Aufgaben, Log `llm_circuit_open` | LLM-Endpunkt nicht erreichbar oder Modell fehlt. Ollama prüfen bzw. Modell laden; die Mails werden automatisch erneut verarbeitet (Abschnitt 8.1). |
| `setup_token … must be at least 32 characters` (api startet nicht) | `OLLAMAIL_SETUP_TOKEN` zu kurz: `openssl rand -hex 16` eintragen oder leeren. |
| `uses the placeholder password 'change-me'` (api/migrate starten nicht) | Datenbank-Passwort ändern, [6.6](#66-upgrade-hinweise-sichere-standardwerte-143). |
| UI aus dem LAN nicht mehr erreichbar | Port ist standardmäßig nur an `127.0.0.1` gebunden; `OLLAMAIL_HTTP_BIND` setzen, [6.6](#66-upgrade-hinweise-sichere-standardwerte-143). |
| Postfach im LAN meldet `connection_failed`, Log `mail_destination_refused` | Interne Adresse ohne Freigabe: Host in `OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS` eintragen, [6.6](#66-upgrade-hinweise-sichere-standardwerte-143). |
| CalDAV-Export im LAN meldet `unavailable`, Log `todo_export_destination_refused` | Interne Adresse ohne Freigabe: Host in `OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS` eintragen, [6.6](#66-upgrade-hinweise-sichere-standardwerte-143). |
| `worker` startet ständig neu | `docker compose -f deploy/compose.yaml logs worker`; häufig ein ungültiger Wert in `OLLAMAIL_WORKER_QUEUES`. |
| `toomanyrequests` / `429 Too Many Requests` beim Build oder Pull | Rate-Limit von Docker Hub. Mit `docker login` anmelden oder später erneut versuchen. |
| `failed to bind host port … address already in use` oder `port is already allocated` | Port 8080 ist belegt. `OLLAMAIL_HTTP_PORT` in `deploy/.env` ändern. |
| UI lädt hinter dem Reverse Proxy, Live-Updates bleiben aus | Pufferung im äußeren Proxy aktiv. Abschnitt 4 (SSE). |
| `could not select device driver "nvidia"` | NVIDIA Container Toolkit fehlt oder Docker wurde danach nicht neu gestartet. Abschnitt 3.3. |
| Container meldet `Read-only file system` | Gewollt: Root-Dateisystem ist schreibgeschützt, beschreibbar sind nur `/tmp` und `/data`. |

Für ausführlichere Logs vorübergehend `OLLAMAIL_LOG_LEVEL=DEBUG` setzen und
`docker compose -f deploy/compose.yaml up -d` ausführen. Auch dann enthalten die Logs keine
Mail-Inhalte.
