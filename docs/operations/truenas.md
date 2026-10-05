# Betrieb auf TrueNAS SCALE

ollamail läuft auf TrueNAS SCALE ab **24.10 „Electric Eel“**. Seit dieser Version sind TrueNAS-Apps
Docker-Compose-Projekte. Es gibt zwei Wege:

| Weg | Status | Datei |
|---|---|---|
| **Custom App** (*Install via YAML*) | sofort nutzbar, sobald die Images öffentlich sind | [`deploy/truenas/compose.yaml`](../../deploy/truenas/compose.yaml) |
| **Katalog-App** (Community-Train von [`truenas/apps`](https://github.com/truenas/apps)) | vorbereitet, noch nicht eingereicht | [`deploy/truenas/app/`](../../deploy/truenas/app) |

Anwendung, Images und Einstellungen sind dieselben wie im Compose-Stack
([`deploy/README.md`](../../deploy/README.md), [`OPERATIONS.md`](../OPERATIONS.md)). Alle Beispiele
nutzen Platzhalter (`tank`, `192.0.2.10`, `mail.example.org`).

## Inhalt

1. [Voraussetzungen](#1-voraussetzungen)
2. [Datasets anlegen](#2-datasets-anlegen)
3. [Installation als Custom App](#3-installation-als-custom-app)
4. [LLM: gebündeltes Ollama, vorhandener Server, GPU](#4-llm-gebündeltes-ollama-vorhandener-server-gpu)
5. [Reverse Proxy und TLS](#5-reverse-proxy-und-tls)
6. [Updates](#6-updates)
7. [Backup und Restore](#7-backup-und-restore)
8. [Katalog-App vorbereiten und einreichen](#8-katalog-app-vorbereiten-und-einreichen)
9. [Troubleshooting](#9-troubleshooting)
10. [Test in der CI](#10-test-in-der-ci)

## 1. Voraussetzungen

- TrueNAS SCALE 24.10 oder neuer, ein Pool für Apps (*Apps → Configuration → Choose Pool*).
- **Öffentliche Images in GHCR.** TrueNAS zieht `ghcr.io/dusseligerdussel/ollamail-api` und
  `ollamail-frontend` ohne Anmeldung. Die Pakete sind derzeit noch privat
  ([`deploy/README.md`](../../deploy/README.md#zugriff-auf-die-images)); bis der Repository-Owner
  sie öffentlich schaltet, bricht die Installation mit `unauthorized` ab. Übergangsweise lässt
  sich TrueNAS bei `ghcr.io` anmelden (*Apps → Configuration → Docker Registries*, in älteren
  Versionen ggf. anders benannt; Benutzer und PAT (classic) mit `read:packages`). Für die
  Katalog-App müssen die Pakete öffentlich sein.
- **Ein Image-Tag.** Die Dateien nutzen das Release `0.1.0`. Neuere Releases stehen unter
  [Releases](https://github.com/dusseligerdussel/ollamail/releases); `edge` (nächtlicher Build von
  `main`) ist ungetestet und nicht für den Produktivbetrieb.
- RAM und CPU wie in [`OPERATIONS.md` §1 und §3](../OPERATIONS.md#3-hardware-profile-und-llm):
  der Basis-Stack ist schlank, das LLM braucht den Großteil.
- Ein freier Port für die UI, Standard **30580**.

## 2. Datasets anlegen

Unter *Datasets* drei Datasets anlegen, z. B. unter `tank/ollamail`. **Dataset-Preset: „Apps“**
(oder „Generic“). Das Preset „SMB“ nicht verwenden: Seine ACL verhindert, dass PostgreSQL sein
Datenverzeichnis auf `0700` setzt.

| Dataset | Pfad im Container | Inhalt | Besitzer (setzt die App) |
|---|---|---|---|
| `tank/ollamail/postgres` | `/var/lib/postgresql/data` | Datenbank | `999:999` (postgres) |
| `tank/ollamail/data` | `/data` | Anhänge, Audio-Digests, TTS-Stimmen | `10001:10001` |
| `tank/ollamail/ollama` | `/root/.ollama` | Modelle des gebündelten Ollama (nur Option A) | `0:0` |

**Rechte:** `api`, `worker` und `frontend` laufen fest als UID/GID **10001** mit schreibgeschütztem
Dateisystem, nicht als TrueNAS-Nutzer `apps` (568). Der One-Shot-Container `permissions` startet
vor allen anderen, prüft den Besitzer von `data` und `ollama` und setzt ihn nur bei Abweichung
(`chown -R`, mit den Capabilities `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, ohne Netzwerk). PostgreSQL
legt sein Unterverzeichnis `pgdata` selbst mit dem richtigen Besitzer an. Eigene ACL-Einträge sind
nicht nötig; wer sie setzt, muss UID 10001 Schreibrechte auf `data` geben.

## 3. Installation als Custom App

1. Secrets erzeugen (z. B. in *System → Shell* oder auf dem eigenen Rechner):

   ```sh
   openssl rand -base64 32   # OLLAMAIL_SECRET_KEY
   openssl rand -hex 24      # Datenbank-Passwort (Hex: keine URL-Kodierung nötig)
   ```

   **Den Secret Key zusätzlich außerhalb von TrueNAS aufbewahren** (Passwort-Manager). Ohne ihn
   sind gespeicherte Zugangsdaten nach einem Restore verloren ([`OPERATIONS.md` §7](../OPERATIONS.md#7-schlüsselverwaltung)).
2. [`deploy/truenas/compose.yaml`](../../deploy/truenas/compose.yaml) kopieren und ersetzen:

   | Platzhalter | Wert | Vorkommen |
   |---|---|---|
   | `CHANGEME_POOL` | Name des Pools, z. B. `tank` | Pfade `/mnt/CHANGEME_POOL/ollamail/…` |
   | `CHANGEME_SECRET_KEY` | Ausgabe von `openssl rand -base64 32` | einmal |
   | `CHANGEME_DB_PASSWORD` | Ausgabe von `openssl rand -hex 24` | **dreimal** (Postgres, `permissions`, Datenbank-URL) |

   Bleibt ein `CHANGEME` stehen, bricht `permissions` mit
   `ollamail: replace every CHANGEME value in the app YAML` ab und nichts weiter startet.
   Weitere Einstellungen aus [`deploy/.env.example`](../../deploy/.env.example) unter
   `x-app-environment` ergänzen, z. B. `OLLAMAIL_AUTH_PUBLIC_URL: https://mail.example.org`.
   Ein `$` in einem Wert muss als `$$` geschrieben werden: TrueNAS startet die Datei mit
   `docker compose`, ohne `.env`-Datei und ohne Variablen.
3. *Apps → Discover Apps → ⋮ → Install via YAML*, Name `ollamail`, YAML einfügen, *Save*.
4. Start abwarten. TrueNAS legt das Compose-Projekt `ix-ollamail` an; die Container heißen
   `ix-ollamail-<dienst>-1`. Der Reihe nach: `permissions` (beendet sich), `postgres` (healthy),
   `migrate` (beendet sich mit `0`), `api` und `worker`, danach `frontend`. Beendete One-Shots
   sind korrekt.
5. Prüfen (*System → Shell*):

   ```sh
   curl http://127.0.0.1:30580/api/healthz   # {"status":"ok"}
   curl http://127.0.0.1:30580/api/readyz    # {"status":"ok","checks":{"database":"ok"}}
   ```

6. **Erst-Admin:** Den Setup-Token aus dem Log des Containers `api` lesen (App-Ansicht in
   *Apps*, Eintrag `setup_pending`, Feld `setup_code`) oder in der Shell:

   ```sh
   sudo docker exec ix-ollamail-api-1 python -m app.cli setup-token
   ```

   Danach über HTTPS ([Abschnitt 5](#5-reverse-proxy-und-tls)) `/setup` öffnen. Notfallzugang:
   `sudo docker exec -it ix-ollamail-api-1 python -m app.cli reset-password`.

Die Custom App gleicht [`deploy/compose.yaml`](../../deploy/compose.yaml): dieselben Dienste,
dieselbe Härtung (schreibgeschütztes Dateisystem, keine Capabilities, `no-new-privileges`),
dasselbe pgvector-Tuning ([`OPERATIONS.md` §8.4](../OPERATIONS.md#84-postgresql-tuning-pgvector))
und dieselben drei Netze ([`deploy/README.md`](../../deploy/README.md#netze)): `edge`
(`frontend`, `api`), `backend` (intern, ohne Route nach außen: `postgres`, Ollama, Backend-Dienste)
und `egress` (`api`, `worker`, Ollama). Veröffentlicht ist nur der Port von `frontend`.

## 4. LLM: gebündeltes Ollama, vorhandener Server, GPU

**Option A – gebündeltes Ollama (Standard).** Der Dienst `ollama` läuft auf der CPU, Modelle liegen
im Dataset `ollama`. Modelle lädt der Worker beim Start automatisch
(`OLLAMAIL_LLM_PULL_MISSING_MODELS`, Standard an) oder der Admin unter *Admin → KI*; von Hand:

```sh
sudo docker exec ix-ollamail-ollama-1 ollama pull <modell>
```

**Option B – vorhandener Server.** Z. B. die Ollama-App aus dem TrueNAS-Katalog (Standard-Port
30068) oder ein Server im Netz ([`OPERATIONS.md` §3.5](../OPERATIONS.md#35-externer-ollama-server)):
den Dienst `ollama` samt Block aus der YAML löschen und `OLLAMAIL_LLM_BASE_URL` setzen, z. B.
`http://192.0.2.10:30068`. Die Ollama-App hat keine Authentifizierung; ihren Port nur im
vertrauenswürdigen Netz erreichbar machen.

**NVIDIA-GPU (Option A).** Treiber installieren (*System → Advanced Settings → NVIDIA Drivers →
Install NVIDIA Drivers*; der Ort hängt von der TrueNAS-Version ab), den auskommentierten `deploy:`-Block am Dienst `ollama` aktivieren und
`OLLAMAIL_LLM_PROFILE: consumer-gpu` (bzw. `server-gpu`) setzen. Die Syntax entspricht dem, was die
TrueNAS-Katalog-Bibliothek für NVIDIA-GPUs erzeugt (`driver: nvidia`, `capabilities: [gpu]`); auf
echter Hardware ist sie in diesem Projekt noch nicht getestet. In der Katalog-App wählt man die GPU
unter *Resources Configuration*. Intel- und AMD-GPUs unterstützt das Ollama-Standard-Image nicht
(AMD bräuchte das `-rocm`-Image); sie bleiben ungenutzt.

## 5. Reverse Proxy und TLS

Der Port 30580 spricht **unverschlüsseltes HTTP** auf allen Adressen des NAS. Sitzungs- und
CSRF-Cookie sind `Secure`: über `http://192.0.2.10:30580` schlagen Setup und Anmeldung fehl
([`OPERATIONS.md` §2.6](../OPERATIONS.md#26-http-ohne-tls-testbetrieb)). Davor gehört ein
TLS-terminierender Reverse Proxy, z. B. eine Proxy-App aus dem TrueNAS-Katalog (Caddy, Traefik,
Nginx Proxy Manager) oder ein vorhandener Proxy im Netz:

- Ziel: `http://<truenas-ip>:30580`, gesamter Pfad `/`. Alternativ hängt sich ein Proxy-Container an
  das Netz `ix-ollamail_edge` und spricht `http://frontend:8080` an – nie an `ix-ollamail_backend`.
- Anforderungen wie in [`OPERATIONS.md` §4](../OPERATIONS.md#4-reverse-proxy-und-tls):
  `X-Forwarded-For` auf die echte Client-IP **setzen** (nicht anhängen), `X-Forwarded-Proto`
  setzen, Server-Sent Events nicht puffern, lange Verbindungen erlauben.
- **HSTS ist Pflicht im Proxy** (`Strict-Transport-Security: max-age=31536000; includeSubDomains`,
  Beispiele in [`OPERATIONS.md` §4.4](../OPERATIONS.md#44-sicherheits-header)). Kann der Proxy keine
  Header setzen: `OLLAMAIL_HSTS` am Dienst `frontend` (Katalog-App: Feld *Strict-Transport-Security*),
  nur bei Zugriff ausschließlich über HTTPS.
- `OLLAMAIL_AUTH_PUBLIC_URL` auf die HTTPS-Adresse setzen (Anmeldung über Identity-Provider, Passkeys).

Soll der Port nur für den Proxy erreichbar sein, in der YAML eine Adresse davorsetzen, z. B.
`"192.0.2.10:30580:8080"`, und das Netz entsprechend absichern. Nur zum Ausprobieren im
vertrauenswürdigen Heimnetz: `OLLAMAIL_AUTH_COOKIE_SECURE: "false"` (Risiken in §2.6).

## 6. Updates

Vorher ein Backup ([Abschnitt 7](#7-backup-und-restore)) und die Release-Notes lesen.

- **Custom App:** *Apps → ollamail → Edit*, in beiden Zeilen `ghcr.io/dusseligerdussel/…:<tag>`
  den Tag ändern (`x-app` und `frontend`), *Save*. TrueNAS zieht die Images und startet neu;
  `migrate` bringt das Schema vor `api` und `worker` auf den neuen Stand. Bei einem gleitenden Tag
  (`latest`, `edge`) genügt *Update* bzw. das Neuziehen der Images.
- **Drittanbieter-Images** (`pgvector/pgvector`, `ollama/ollama`) sind wie im Compose-Stack
  gepinnt; Wechsel siehe [`OPERATIONS.md` §6.5](../OPERATIONS.md#65-drittanbieter-images). Den
  PostgreSQL-Major nie durch bloßes Ändern des Tags anheben (Dump und Restore).
- **Rollback:** alten Tag eintragen und das Datenbank-Backup des alten Stands einspielen
  ([`OPERATIONS.md` §6.4](../OPERATIONS.md#64-rollback-auf-die-vorherige-version)).

## 7. Backup und Restore

Ein vollständiges Backup besteht wie im Compose-Stack aus **drei Teilen**
([`OPERATIONS.md` §5](../OPERATIONS.md#5-backup-und-restore)):

| Teil | Auf TrueNAS |
|---|---|
| Konfiguration inkl. Secret Key | eine Kopie der App-YAML (*Apps → ollamail → Edit*), sicher außerhalb von TrueNAS aufbewahrt; sie enthält Secret Key und Datenbank-Passwort |
| Datenbank | `pg_dump` (konsistent im laufenden Betrieb) und/oder Snapshot des Datasets `postgres` |
| Daten | Snapshot des Datasets `data` |

```sh
# Datenbank-Dump, z. B. als Cron Job (System → Advanced Settings → Cron Jobs)
sudo docker exec ix-ollamail-postgres-1 \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > /mnt/tank/backup/ollamail-db.dump
```

**Snapshots:** Eine periodische Snapshot-Aufgabe (*Data Protection → Periodic Snapshot Tasks*)
für `tank/ollamail` mit *Recursive* sichert `postgres` und `data` gemeinsam. Ein Snapshot einer
laufenden Datenbank ist absturzkonsistent; PostgreSQL startet daraus wie nach einem Stromausfall.
Für ein garantiert konsistentes Backup zusätzlich den `pg_dump` oben nutzen oder die App vor dem
Snapshot stoppen. `ollama` muss nicht gesichert werden. Replikation (*Replication Tasks*) auf ein
zweites System schützt vor dem Verlust des Pools. Das Backup enthält Mail-Daten: verschlüsselt
und getrennt vom Secret Key aufbewahren.

**Restore** auf einer leeren Instanz: App mit derselben YAML (gleicher Secret Key, gleiches
Datenbank-Passwort) anlegen, stoppen, Datasets aus dem Snapshot zurückrollen oder klonen und die App
starten – oder bei einem Dump: nur `postgres` laufen lassen und den Dump wie in
[`OPERATIONS.md` §5.2](../OPERATIONS.md#52-restore-auf-einer-leeren-instanz) mit
`sudo docker exec -i ix-ollamail-postgres-1 pg_restore …` einspielen.

## 8. Katalog-App vorbereiten und einreichen

[`deploy/truenas/app/`](../../deploy/truenas/app) enthält die App im Format von
[`truenas/apps`](https://github.com/truenas/apps/blob/master/CONTRIBUTIONS.md) (Ordner
`ix-dev/community/ollamail`):

| Datei | Inhalt |
|---|---|
| `app.yaml` | Metadaten (Version, `lib_version` der TrueNAS-Bibliothek, `run_as_context`) |
| `item.yaml` | Katalogeintrag (Kategorien, Icon, Tags) |
| `ix_values.yaml` | Images und Konstanten (Containernamen, UID 10001) |
| `questions.yaml` | Formular in der TrueNAS-UI: Secret Key, Datenbank-Passwort, LLM (gebündelt/extern, Profil), Public URL, Port, Speicher, GPU, Limits |
| `templates/docker-compose.yaml` | Jinja2-Template auf Basis der Bibliothek `ix_lib` |
| `templates/test_values/*.yaml` | Testwerte: externes LLM bzw. gebündeltes Ollama |

Unterschiede zur Custom App: Postgres kommt aus der Bibliothek (`tpl.deps.postgres`, mit
Upgrade-Helfer und `PGDATA=/var/lib/postgresql/16/docker`), die Rechte setzt der Container
`permissions` von TrueNAS (`ixsystems/container-utils`), Speicher ist wahlweise ixVolume oder Host
Path. Zwei Abweichungen von den Konventionen des Katalogs sind bewusst und im PR an `truenas/apps`
zu begründen:

- **Feste UID 10001** statt der wählbaren 568: Die Images legen Nutzer und Dateirechte fest.
- **`internal: true` für das Netz `backend`:** Die Bibliothek kennt keine Netze ohne Route nach
  außen; das Template setzt es nach dem Rendern, damit PostgreSQL und Ollama wie im Compose-Stack
  isoliert sind.

**Lokal prüfen** (Docker, Python 3.11+, git):

```sh
scripts/truenas-app.sh /tmp/ollamail-truenas
```

Das Skript holt `truenas/apps` und `truenas/apps_validation` in festen Versionen, legt die App als
`ix-dev/community/ollamail` mit der Bibliothek aus `lib_version` ab, prüft `app.yaml`,
`questions.yaml` und die Dateien mit dem Validator von TrueNAS, rendert das Template mit jeder
Testwerte-Datei und prüft das Ergebnis mit `docker compose config`. Es veröffentlicht nichts.

**Einreichen** (macht der Repository-Owner, kein Agent):

1. Voraussetzungen: Images öffentlich, der aktuelle Release-Tag in `ix_values.yaml` und als
   `app_version` in `app.yaml`, ein Icon (SVG/PNG) und Screenshots mit Testdaten.
2. Laut `CONTRIBUTIONS.md` vorher ein Issue in `truenas/apps` öffnen und die Aufnahme abstimmen;
   TrueNAS entscheidet nach eigenem Ermessen.
3. `truenas/apps` forken, `deploy/truenas/app/` nach `ix-dev/community/ollamail/` kopieren.
4. Im Fork (Werkzeuge über `devbox`, siehe dortiges `devbox.json`):
   - `devbox run copy-lib` – kopiert die Bibliothek nach `templates/library/` und setzt
     `lib_version_hash` (wird mit eingecheckt),
   - `./.github/scripts/generate_metadata.py` – erzeugt `capabilities` und `item.yaml` neu,
   - `./.github/scripts/port_validation.py` – der Standard-Port 30580 muss im Katalog frei sein
     (bei der Vorbereitung war er es),
   - `./.github/scripts/ci.py --app ollamail --train community --test-file basic-values.yaml` –
     rendert, startet und wartet auf gesunde Container (auch mit `bundled-ollama-values.yaml`).
5. Für Tags, die nicht SemVer sind, braucht Renovate dort eine Regel (`.github/renovate-config.js`).
6. PR mit der Vorlage `app_addition.md` stellen; Icon und Screenshots als Anhang (TrueNAS lädt sie
   auf `media.sys.truenas.net` hoch). Das Feld „AI / LLM generated“ wahrheitsgemäß ankreuzen.
7. Nach der Aufnahme pflegt die App der Katalog; Änderungen hier (`deploy/truenas/app/`) danach
   als PR dort nachziehen und `version` in `app.yaml` erhöhen.

## 9. Troubleshooting

| Symptom | Ursache und Lösung |
|---|---|
| `permissions` beendet sich mit `replace every CHANGEME value` | Platzhalter stehen noch in der YAML ([Abschnitt 3](#3-installation-als-custom-app)). |
| Installation bricht mit `unauthorized` / `denied` beim Image-Pull ab | GHCR-Pakete sind privat ([Abschnitt 1](#1-voraussetzungen)). |
| `postgres` startet nicht, `data directory … has invalid permissions` | Dataset mit SMB-Preset bzw. restriktiver ACL; mit Preset „Apps“ oder „Generic“ neu anlegen. |
| `api` meldet `Permission denied` unter `/data` | Eigene ACL ohne Schreibrecht für UID 10001; ACL entfernen oder 10001 eintragen, App neu starten. |
| Anmeldung schlägt mit `csrf_failed` fehl | Zugriff über HTTP; Reverse Proxy mit TLS ([Abschnitt 5](#5-reverse-proxy-und-tls)). |
| `port is already allocated` | Port 30580 belegt; in der YAML z. B. `"30581:8080"` eintragen. |
| `Invalid compose configuration` beim Speichern | YAML-Syntax, oder ein `$` in einem Wert ist nicht als `$$` geschrieben. |

Weitere Fehlerbilder: [`OPERATIONS.md` §10](../OPERATIONS.md#10-troubleshooting).

## 10. Test in der CI

Der Job „Compose smoke test“ ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) prüft
nach dem regulären Stack beide Varianten ohne TrueNAS, nur mit Docker:

- **Custom App:** `deploy/truenas/compose.yaml` mit unveränderten Platzhaltern muss scheitern;
  mit ersetzten Platzhaltern (Datasets als Verzeichnisse mit Besitzer 568, Projektname
  `ix-ollamail`, frisch gebaute Images unter den GHCR-Namen, ohne Ollama) müssen `/api/healthz`,
  `/api/readyz` und die UI antworten, `migrate` und `permissions` mit `0` enden, `/data` UID 10001
  gehören und die Netztrennung stimmen.
- **Katalog-App:** `scripts/truenas-app.sh` validiert und rendert die App; der gerenderte Stack
  (Testwerte `basic-values.yaml`, externes LLM) muss ebenso starten.

`scripts/check-image-pins.sh` prüft, dass die gepinnten Drittanbieter-Images in beiden
TrueNAS-Dateien mit `deploy/compose.yaml` übereinstimmen.
