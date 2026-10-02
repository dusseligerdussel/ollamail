# Betrieb auf Kubernetes (Helm)

Das Helm-Chart [`deploy/helm/ollamail`](../../deploy/helm/ollamail) richtet sich an Unternehmen,
die ollamail auf Kubernetes betreiben. Für Self-Hoster bleibt Docker Compose der Standard
([`deploy/README.md`](../../deploy/README.md), [`OPERATIONS.md`](../OPERATIONS.md)); Anwendung,
Images und Einstellungen sind identisch.

## Inhalt

1. [Überblick](#1-überblick)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Installation](#3-installation)
4. [Datenbank](#4-datenbank)
5. [Secrets](#5-secrets)
6. [Einstellungen](#6-einstellungen)
7. [Worker und Queues](#7-worker-und-queues)
8. [LLM: Ollama oder externer Endpunkt](#8-llm-ollama-oder-externer-endpunkt)
9. [Speicher](#9-speicher)
10. [Ingress und TLS](#10-ingress-und-tls)
11. [Sicherheit und NetworkPolicies](#11-sicherheit-und-networkpolicies)
12. [Updates, Migrationen, Betrieb](#12-updates-migrationen-betrieb)
13. [Test und CI](#13-test-und-ci)

## 1. Überblick

| Ressource | Name | Aufgabe |
|---|---|---|
| Deployment + Service | `<release>-frontend` | UI (Caddy), Reverse Proxy `/api/*` → `<release>-api:8000`, Port 8080 |
| Deployment + Service | `<release>-api` | FastAPI, Port 8000, Probes `/healthz` und `/readyz` |
| Deployment je Worker-Gruppe | `<release>-worker-<gruppe>` | Hintergrundjobs (`python -m app.worker`), Queues je Gruppe |
| Job (Helm-Hook) | `<release>-migrate` | `alembic upgrade head` vor jeder Installation und jedem Upgrade |
| PersistentVolumeClaim | `<release>-data` | `/data` in `api` und allen Workern (Anhänge, Audio, TTS-Stimmen, Exporte) |
| Ingress (optional) | `<release>` | Leitet alles an das Frontend, optional mit cert-manager |
| Deployment + Service + PVC (optional) | `<release>-ollama` | Ollama auf CPU oder GPU |
| NetworkPolicies (optional) | `<release>-*` | Eingehend nur, was nötig ist; ausgehend optional eingeschränkt |
| Pod (Helm-Test) | `<release>-test-readyz` | `helm test`: `/api/readyz` über das Frontend muss `200` liefern |

Wie im Compose-Stack ist das Frontend der einzige Einstiegspunkt; die API ist nur
clusterintern erreichbar. Es gibt keinen Redis und keinen eigenen Scheduler: Queue,
periodische Jobs und Pub/Sub laufen über PostgreSQL ([`ARCHITECTURE.md`](../ARCHITECTURE.md#1-komponenten)).

## 2. Voraussetzungen

- Kubernetes ≥ 1.27, Helm 3 (getestet im CI mit Helm 3.22 und kind).
- **PostgreSQL 16 mit der Extension `pgvector`** (Pflicht, siehe [Abschnitt 4](#4-datenbank)).
- Ein Secret mit `OLLAMAIL_SECRET_KEY` ([Abschnitt 5](#5-secrets)).
- Eine StorageClass für das Daten-Volume, bei mehreren Nodes mit `ReadWriteMany`
  ([Abschnitt 9](#9-speicher)).
- Ein LLM-Endpunkt: das optionale Ollama aus dem Chart oder ein vorhandener Ollama-/vLLM-/
  OpenAI-kompatibler Server ([Abschnitt 8](#8-llm-ollama-oder-externer-endpunkt)).
- Zugriff auf die Images in GHCR. Solange das Repository privat ist, sind auch die Pakete
  privat: Pull-Secret anlegen und eintragen:

  ```sh
  kubectl -n ollamail create secret docker-registry ghcr \
    --docker-server=ghcr.io --docker-username=<github-benutzer> \
    --docker-password=<PAT (classic) mit read:packages>
  ```

  ```yaml
  imagePullSecrets:
    - name: ghcr
  ```

  Alternativ die Images in eine eigene Registry spiegeln und `image.backend.repository` /
  `image.frontend.repository` anpassen.

## 3. Installation

Das Chart liegt im Repository (noch nicht in einem Helm-Repository oder als OCI-Artefakt
veröffentlicht).

```sh
kubectl create namespace ollamail

# 1. Datenbank bereitstellen (Abschnitt 4) – hier: externe Datenbank, URL im App-Secret
# 2. App-Secret anlegen (Abschnitt 5)
kubectl -n ollamail create secret generic ollamail-secrets \
  --from-literal=OLLAMAIL_SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=OLLAMAIL_DATABASE_URL='postgresql+asyncpg://ollamail:<passwort>@db.example.internal:5432/ollamail'

# 3. Eigene Werte, z. B. values-prod.yaml (Beispiel unten)
helm install ollamail deploy/helm/ollamail -n ollamail -f values-prod.yaml --wait

# 4. Prüfen
helm -n ollamail test ollamail
```

Beispiel `values-prod.yaml`:

```yaml
image:
  backend:
    tag: "1.2.3"        # feste Version statt latest
  frontend:
    tag: "1.2.3"
imagePullSecrets:
  - name: ghcr
secrets:
  existingSecret: ollamail-secrets
config:
  OLLAMAIL_AUTH_PUBLIC_URL: https://mail.example.com
  OLLAMAIL_LLM_BASE_URL: http://ollama.llm.svc.cluster.local:11434
ingress:
  enabled: true
  className: nginx
  hosts: [mail.example.com]
  certManager:
    enabled: true
    issuerName: letsencrypt
persistence:
  storageClass: nfs-client
  accessModes: [ReadWriteMany]
```

Danach im Browser den Einrichtungsassistenten öffnen und den ersten Admin anlegen. Der dafür
nötige Setup-Code (falls `OLLAMAIL_SETUP_TOKEN` nicht gesetzt ist):

```sh
kubectl -n ollamail exec deploy/ollamail-api -- python -m app.cli setup-token
```

Alle Werte mit Kommentaren: [`values.yaml`](../../deploy/helm/ollamail/values.yaml). Ohne
`image.*.tag` nutzt das Chart seine `appVersion` (derzeit `latest`, bis es ein erstes Release
gibt); bewegliche Tags (`latest`, `edge`) werden mit `imagePullPolicy: Always` gezogen.

## 4. Datenbank

ollamail braucht **PostgreSQL 16 mit `pgvector`**. Die erste Migration führt
`CREATE EXTENSION IF NOT EXISTS vector` aus; ist der Datenbanknutzer kein Superuser, die
Extension vorher als Superuser anlegen:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Verwaltete Dienste (Azure Database for PostgreSQL, Amazon RDS/Aurora, Google Cloud SQL)
bieten `vector` an; bei Azure muss sie vorher in `azure.extensions` freigeschaltet werden.

Das Chart liest die Verbindung (`OLLAMAIL_DATABASE_URL`, Schema `postgresql+asyncpg://`) aus
**genau einer** Quelle:

| Variante | Werte | Bemerkung |
|---|---|---|
| URL im App-Secret (Standard) | – | Schlüssel `OLLAMAIL_DATABASE_URL` in `secrets.existingSecret` |
| URL in eigenem Secret | `database.existingSecret`, `database.existingSecretKey` | z. B. von External Secrets befüllt |
| CloudNativePG | `database.cloudnativepg.cluster` | Host `<cluster>-rw`, Zugangsdaten aus dem vom Operator erzeugten Secret `<cluster>-app` |
| Einzelwerte | `database.external.host`, `.port`, `.name`, `.user`, `.passwordSecret`, `.passwordSecretKey` | Passwort aus Secret; nur URL-sichere Zeichen, sonst Variante 1 oder 2 |

**Empfohlen ist eine externe, gesicherte Datenbank** (verwalteter Dienst oder eigener Cluster
mit Backups). Das Chart installiert selbst keine Datenbank.

### CloudNativePG

[CloudNativePG](https://cloudnative-pg.io) ist der empfohlene Weg, PostgreSQL im selben
Kubernetes-Cluster zu betreiben (Backups, Failover, Updates über den Operator).

```sh
# Operator (einmal pro Cluster), siehe https://cloudnative-pg.io/documentation/current/installation_upgrade/
kubectl apply --server-side -f \
  https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml

# Datenbank-Cluster mit pgvector im Namespace von ollamail
kubectl -n ollamail apply -f deploy/helm/examples/cnpg-cluster.yaml
kubectl -n ollamail wait cluster/ollamail-db --for=condition=Ready --timeout=5m

helm install ollamail deploy/helm/ollamail -n ollamail \
  --set secrets.existingSecret=ollamail-secrets \
  --set database.cloudnativepg.cluster=ollamail-db
```

[`examples/cnpg-cluster.yaml`](../../deploy/helm/examples/cnpg-cluster.yaml) nutzt das
„standard“-Image von CloudNativePG (enthält pgvector) und legt die Extension beim
Bootstrap an. Für den Produktivbetrieb Instanzen (≥ 2), Speichergröße und Backups
(Barman Cloud Plugin oder Volume Snapshots) ergänzen. Der Cluster ist bewusst **nicht** Teil
des Helm-Releases: So überlebt er ein `helm uninstall`, und die Migration kann vor der
Installation laufen.

### Bitnami

Eine Bitnami-Abhängigkeit bietet das Chart nicht an: Bitnami hat 2025 seinen freien
Image-Katalog eingestellt (Images ohne Updates nur noch unter `bitnamilegacy`). Ein bereits
vorhandenes Bitnami-PostgreSQL mit pgvector lässt sich über `database.external.*`
anbinden.

### Verbindungen

Jeder API- und Worker-Prozess öffnet bis zu `OLLAMAIL_DATABASE_POOL_SIZE +
OLLAMAIL_DATABASE_MAX_OVERFLOW` Verbindungen (Standard 5 + 10), Worker zusätzlich einige für
die Job-Queue. Bei vielen Replikaten `max_connections` der Datenbank anpassen oder einen
Pooler vorschalten (CloudNativePG: `Pooler`, Modus `session` – `LISTEN/NOTIFY` und
Advisory-Locks funktionieren nicht im `transaction`-Modus).

## 5. Secrets

Das Chart erzeugt **keine** Secrets und kennt keine Default-Werte für Schlüssel oder
Passwörter. `secrets.existingSecret` ist Pflicht; alle Schlüssel dieses Secrets werden als
Umgebungsvariablen an `api`, Worker und Migrations-Job übergeben (`envFrom`).

| Schlüssel | Pflicht | Inhalt |
|---|---|---|
| `OLLAMAIL_SECRET_KEY` | ja | Master-Key, `openssl rand -base64 32` (siehe [`PRIVACY.md`](../PRIVACY.md)) |
| `OLLAMAIL_DATABASE_URL` | je nach Datenbank-Variante | siehe [Abschnitt 4](#4-datenbank) |
| `OLLAMAIL_SECRET_KEYS_OLD` | nur bei Key-Rotation | frühere Master-Keys, kommagetrennt |
| `OLLAMAIL_SETUP_TOKEN` | nein | fester Setup-Token für den ersten Admin |
| `OLLAMAIL_LLM_API_KEY`, `OLLAMAIL_LLM_ENDPOINTS` | nein | API-Keys von LLM-Endpunkten |
| `OLLAMAIL_AUTH_OIDC_PROVIDERS` | nein | OIDC-Provider inkl. Client-Secret (GitOps) |
| `OLLAMAIL_MAIL_GRAPH_CLIENT_SECRET`, `OLLAMAIL_GMAIL_CLIENT_SECRET` | nein | OAuth-Client-Secrets |

Mit dem [External Secrets Operator](https://external-secrets.io) kommt das Secret aus Vault,
Azure Key Vault, AWS Secrets Manager usw.:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: ollamail-secrets
  namespace: ollamail
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault
  target:
    name: ollamail-secrets
  data:
    - secretKey: OLLAMAIL_SECRET_KEY
      remoteRef:
        key: ollamail/master-key
    - secretKey: OLLAMAIL_DATABASE_URL
      remoteRef:
        key: ollamail/database-url
```

Weitere Secrets (z. B. ein zweites für OIDC) über `extraEnvFrom`, einzelne Werte über
`extraEnv` mit `secretKeyRef`. Dateien wie den Gmail-Service-Account-Schlüssel
(`OLLAMAIL_GMAIL_SERVICE_ACCOUNT_FILE`) oder ein CA-Zertifikat für den Mailserver
(`SSL_CERT_FILE`) über `extraVolumes` / `extraVolumeMounts` einbinden.

Pods starten neu, wenn sich die Pod-Spezifikation ändert, nicht wenn sich der Inhalt eines
Secrets ändert. Nach einer Änderung: `kubectl -n ollamail rollout restart deploy`.

**Key-Rotation** (Ablauf wie in [`deploy/README.md`](../../deploy/README.md#master-key-und-key-rotation)):
alten Key nach `OLLAMAIL_SECRET_KEYS_OLD` verschieben, neuen als `OLLAMAIL_SECRET_KEY` setzen,
neu starten, dann

```sh
kubectl -n ollamail exec deploy/ollamail-api -- python -m app.cli rotate-keys
```

und `OLLAMAIL_SECRET_KEYS_OLD` wieder leeren.

## 6. Einstellungen

Alle Einstellungen aus [`deploy/.env.example`](../../deploy/.env.example) werden unter
`config` gesetzt; das Chart übergibt sie als Umgebungsvariablen an `api`, Worker und
Migrations-Job. Maps und Listen werden als JSON übergeben, Zahlen und Booleans als Text.

```yaml
config:
  OLLAMAIL_LOG_LEVEL: INFO
  OLLAMAIL_AUTH_PUBLIC_URL: https://mail.example.com
  OLLAMAIL_MAIL_INITIAL_SYNC_DAYS: 30
  OLLAMAIL_DIGEST_AUDIO_FORMATS: mp3,opus
  OLLAMAIL_LLM_ENDPOINTS:            # wird zu JSON
    vllm:
      provider: openai_compatible
      base_url: http://vllm.llm.svc.cluster.local:8000/v1
```

Regeln:

- **Geheime Werte gehören nicht in `config`.** Schlüssel wie `OLLAMAIL_SECRET_KEY`,
  `OLLAMAIL_DATABASE_URL` oder alles, was auf `_SECRET`, `_PASSWORD`, `_API_KEY` oder `_TOKEN`
  endet, lehnt das Chart ab ([Abschnitt 5](#5-secrets)). Enthalten `OLLAMAIL_LLM_ENDPOINTS`
  oder `OLLAMAIL_AUTH_OIDC_PROVIDERS` Secrets, ebenfalls ins Secret.
- `OLLAMAIL_DATA_DIR` setzt das Chart selbst (`/data`, [Abschnitt 9](#9-speicher)).
- Die Worker-Variablen `OLLAMAIL_WORKER_QUEUES`, `OLLAMAIL_WORKER_CONCURRENCY` und
  `OLLAMAIL_WORKER_SHUTDOWN_TIMEOUT` kommen aus `worker.*` ([Abschnitt 7](#7-worker-und-queues)).
- Mit `OLLAMAIL_AUTH_COOKIE_SECURE=true` (Standard) funktioniert die Anmeldung nur über HTTPS
  (oder `http://localhost`, z. B. per `kubectl port-forward`).
- Der Frontend-Container trägt `X-Forwarded-*`-Header nur von privaten Netzen weiter; ein
  Ingress-Controller im Cluster erfüllt das.

## 7. Worker und Queues

Jede Gruppe unter `worker.groups` wird ein eigenes Deployment `<release>-worker-<name>` mit
eigenen Queues, Replikaten, Ressourcen und Scheduling. Queues: `sync` (Mail-Sync, IMAP IDLE),
`llm` (LLM-Aufrufe), `tts` (Sprachsynthese), `ocr` (Texterkennung gescannter Anhänge,
eigene Job-Slots `OLLAMAIL_SEARCH_OCR_CONCURRENCY`), `default` (alles andere, periodische Jobs). Jede
Queue muss von mindestens einer Gruppe abgearbeitet werden, sonst bricht die Installation ab.

Standard ist eine Gruppe `all` mit allen Queues. Beispiel mit getrennten LLM-Workern auf
einem GPU-Node-Pool:

```yaml
worker:
  groups:
    - name: main
      replicaCount: 2
      queues: [sync, tts, ocr, default]
    - name: llm
      replicaCount: 2
      queues: [llm]
      llmMaxConcurrency: 8        # OLLAMAIL_LLM_MAX_CONCURRENCY
      nodeSelector: {node-pool: gpu}
      tolerations:
        - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
      resources:
        requests: {cpu: "1", memory: 2Gi}
```

Felder je Gruppe: `name`, `replicaCount`, `queues`, `concurrency` (Standard
`worker.concurrency`), `llmMaxConcurrency`, `config` (zusätzliche Einstellungen nur für diese
Gruppe), `resources`, `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`,
`podAnnotations`, `podLabels`. Nicht gesetzte Felder erben von `worker.*`.

- Mehrere Replikate sind unkritisch: Periodische Jobs werden in der Datenbank dedupliziert,
  Postfächer der Queue `sync` verteilen sich über Advisory-Locks auf die Worker.
- Beim Stoppen bekommen laufende Jobs `worker.shutdownTimeout` Sekunden (Standard 30);
  Kubernetes wartet `worker.terminationGracePeriodSeconds` (Standard 45).
- Der Worker hat keinen HTTP-Endpunkt und deshalb standardmäßig keine Probe. Abstürze
  beenden den Prozess (Exit-Code 1), Kubernetes startet ihn neu.
- Wie viele LLM-Anfragen gleichzeitig laufen, steuert `OLLAMAIL_LLM_CONCURRENCY`
  (zur Laufzeit im Admin-Bereich unter „KI“ änderbar).

## 8. LLM: Ollama oder externer Endpunkt

**Externer Endpunkt** (empfohlen, wenn es im Cluster schon einen LLM-Server gibt):

```yaml
config:
  # Ollama
  OLLAMAIL_LLM_BASE_URL: http://ollama.llm.svc.cluster.local:11434
  # oder vLLM / OpenAI-kompatibel
  # OLLAMAIL_LLM_PROVIDER: openai_compatible
  # OLLAMAIL_LLM_BASE_URL: http://vllm.llm.svc.cluster.local:8000/v1
  OLLAMAIL_LLM_PROFILE: gpu-server
```

**Ollama aus dem Chart** (`ollama.enabled: true`): Deployment `<release>-ollama` mit eigenem
PVC für die Modelle (`ollama.persistence`, Standard 50 Gi, bleibt bei `helm uninstall`
erhalten). `OLLAMAIL_LLM_BASE_URL` zeigt dann automatisch darauf, sofern nicht in `config`
gesetzt.

```yaml
ollama:
  enabled: true
  gpu:
    enabled: true                 # fordert nvidia.com/gpu: 1 an (NVIDIA Device Plugin)
  runtimeClassName: nvidia        # falls der Cluster eine RuntimeClass nutzt
  nodeSelector: {node-pool: gpu}
  tolerations:
    - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
config:
  OLLAMAIL_LLM_PROFILE: gpu-consumer
  OLLAMAIL_LLM_PULL_MISSING_MODELS: true   # fehlende Modelle beim API-Start laden
```

Ohne GPU läuft Ollama auf der CPU (Profil `cpu`, kleine Modelle). Modelle manuell laden:

```sh
kubectl -n ollamail exec deploy/ollamail-ollama -- ollama pull qwen2.5:3b
```

Ollama läuft im Chart als unprivilegierter Nutzer mit schreibgeschütztem Dateisystem;
Schlüssel und Modelle liegen auf dem Volume (`HOME=/ollama`, `OLLAMA_MODELS=/ollama/models`).
Für AMD-GPUs das `rocm`-Image (`ollama.image.tag`) und `ollama.gpu.resourceName:
amd.com/gpu` setzen. Das Ollama-Deployment wird im CI nur gerendert und validiert, nicht
gestartet (Image > 1 GB).

`OLLAMAIL_LLM_READINESS_CHECK=true` nimmt die Erreichbarkeit der Modelle in `/readyz` auf;
dann ist die API erst bereit, wenn das LLM antwortet.

## 9. Speicher

`/data` (Anhänge, Audio-Digests, TTS-Stimmen, Datenexporte) wird von `api` **und** allen
Workern gemountet:

- **Ein Node oder `ReadWriteMany`:** Mit `ReadWriteOnce` (Standard) können alle Pods nur auf
  demselben Node laufen. Bei mehreren Nodes eine RWX-StorageClass (NFS, CephFS, Longhorn RWX,
  Azure Files, Amazon EFS, Filestore) verwenden:
  `persistence.accessModes: [ReadWriteMany]`, `persistence.storageClass: <klasse>`.
- Das PVC trägt `helm.sh/resource-policy: keep` und bleibt bei `helm uninstall` erhalten
  (Datenschutz: nach endgültiger Außerbetriebnahme manuell löschen,
  `kubectl -n ollamail delete pvc ollamail-data`).
- `persistence.existingClaim` bindet ein vorhandenes PVC ein. `persistence.enabled: false`
  nutzt ein `emptyDir` – nur für Tests, Dateien gehen bei jedem Neustart verloren.
- Ohne Internetzugang `OLLAMAIL_TTS_DOWNLOAD_VOICES=false` setzen und die Piper-Stimmen nach
  `/data/tts/voices/piper/` kopieren (siehe [`OPERATIONS.md`](../OPERATIONS.md)).

Backup: Datenbank (Operator-Backups bzw. `pg_dump`) **und** das Daten-Volume (z. B. Velero,
Volume Snapshots); siehe auch [`OPERATIONS.md` §5](../OPERATIONS.md#5-backup-und-restore).

## 10. Ingress und TLS

```yaml
ingress:
  enabled: true
  className: nginx
  hosts: [mail.example.com]
  annotations:
    # Server-Sent Events (RAG-Chat, Job-Fortschritt): nicht puffern, lange Timeouts
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-body-size: 50m
  certManager:
    enabled: true
    issuerName: letsencrypt       # ClusterIssuer (issuerKind: Issuer für einen Namespace-Issuer)
```

Der Ingress leitet alles an das Frontend; das Frontend liefert die UI aus und reicht `/api`
an die API weiter. Mit `certManager.enabled` und leerer `tls`-Liste legt das Chart einen
TLS-Eintrag für alle Hosts an (Secret `<release>-tls`); eigene Zertifikate über `ingress.tls`.
`OLLAMAIL_AUTH_PUBLIC_URL` auf die öffentliche URL setzen (OIDC-Redirects). Für Traefik,
HAProxy oder Gateway-API-Controller entsprechend Buffering und Timeouts anpassen; ohne
Ingress kann der Service `<release>-frontend` auch als `LoadBalancer` laufen
(`frontend.service.type`).

## 11. Sicherheit und NetworkPolicies

Standardmäßig gilt für `api`, Worker, Frontend, Migrations-Job und Test:

- `runAsNonRoot` (UID/GID 10001), `fsGroup` 10001, `seccompProfile: RuntimeDefault`
- `readOnlyRootFilesystem`, beschreibbar sind nur `/tmp` (`emptyDir`) und `/data`
- keine Capabilities, `allowPrivilegeEscalation: false`
- kein ServiceAccount-Token im Pod (ollamail nutzt die Kubernetes-API nicht)

Damit erfüllen die Pods den Pod Security Standard **restricted**
(`pod-security.kubernetes.io/enforce: restricted` am Namespace).

`networkPolicy.enabled: true` (CNI mit NetworkPolicy-Unterstützung nötig) erlaubt eingehend:

| Ziel | Erlaubt von |
|---|---|
| `frontend` :8080 | `networkPolicy.frontend.from` (z. B. Namespace des Ingress-Controllers), leer = alle |
| `api` :8000 | nur `frontend` |
| `worker` | niemand |
| `ollama` :11434 | `api` und Worker |

`networkPolicy.egress.enabled: true` schränkt zusätzlich den ausgehenden Verkehr von `api`,
Workern und Migrations-Job auf DNS und die Pods des Releases ein. Alles andere muss unter
`networkPolicy.egress.extraRules` freigegeben werden: Datenbank, Mailserver (IMAP 993,
Graph/Gmail über 443), Identity-Provider (OIDC 443, LDAPS 636), externe LLM-Endpunkte und der
Download der TTS-Stimmen.

## 12. Updates, Migrationen, Betrieb

```sh
helm upgrade ollamail deploy/helm/ollamail -n ollamail -f values-prod.yaml --wait
```

- Der Job `<release>-migrate` (Helm-Hook `pre-install,pre-upgrade`) führt
  `alembic upgrade head` aus, **bevor** neue Pods starten. Schlägt er fehl, bricht
  Install/Upgrade ab und die alte Version läuft weiter; der fehlgeschlagene Job bleibt bis zum
  nächsten Versuch für die Fehlersuche stehen (`kubectl -n ollamail logs job/ollamail-migrate`).
- Der Hook läuft vor den übrigen Ressourcen des Charts und nutzt deshalb nur vorhandene
  Secrets und den Default-ServiceAccount des Namespaces (`migrations.serviceAccountName`
  für einen eigenen, z. B. für Workload Identity).
- Vor jedem Update ein Backup ziehen; einen automatischen Downgrade gibt es nicht
  ([`OPERATIONS.md` §6](../OPERATIONS.md#6-updates-und-migrationen)).
- Neue Einstellungen stehen im Changelog und in `deploy/.env.example`.

Nützliche Befehle:

```sh
kubectl -n ollamail logs deploy/ollamail-api                    # JSON-Logs ohne Mail-Inhalte
kubectl -n ollamail logs deploy/ollamail-worker-all
kubectl -n ollamail exec deploy/ollamail-api -- alembic current  # Schema-Stand
kubectl -n ollamail exec deploy/ollamail-api -- python -m app.cli --help
helm -n ollamail test ollamail                                   # /api/readyz über das Frontend
```

`/healthz` (Liveness) prüft nur den Prozess, `/readyz` (Readiness) die Datenbank und optional
das LLM. Ist die Datenbank nicht erreichbar, nimmt Kubernetes alle API-Pods aus dem Service,
startet sie aber nicht neu.

## 13. Test und CI

Der Workflow [`.github/workflows/helm.yml`](../../.github/workflows/helm.yml) läuft bei
Änderungen am Chart, an den Dockerfiles, an den Migrationen oder am Workflow selbst:

1. `helm lint --strict` mit Standardwerten und allen Dateien unter `deploy/helm/ci/`.
2. `helm template` mit Standardwerten, den kind-Werten und
   [`ci/values-full.yaml`](../../deploy/helm/ci/values-full.yaml) (alle Optionen an) und
   `kubeconform -strict` gegen Kubernetes 1.27 und 1.34.
3. Negativtests: ungültige Werte (fehlendes Secret, Secret in `config`, nicht abgearbeitete
   Queue, …) müssen abgelehnt werden.
4. Installationstest in **kind**, je einmal mit einfachem pgvector-PostgreSQL
   ([`ci/postgres.yaml`](../../deploy/helm/ci/postgres.yaml)) und mit CloudNativePG: Images
   aus dem aktuellen Commit bauen, `helm install --wait` in einen Namespace mit Pod Security
   Standard `restricted` (inkl. Migrations-Hook, NetworkPolicies, zwei Worker-Gruppen), `helm test`, `/api/readyz` und die UI über
   `kubectl port-forward`, `alembic current` = head, Worker gestartet ohne Neustarts, danach
   `helm upgrade` (Pre-Upgrade-Hook) und erneut `helm test`.

Lokal (Docker, [kind](https://kind.sigs.k8s.io), Helm):

```sh
docker build -t ollamail-api:ci backend
docker build -t ollamail-frontend:ci frontend
kind create cluster --name ollamail
kind load docker-image --name ollamail ollamail-api:ci ollamail-frontend:ci
kubectl create namespace ollamail-db
kubectl -n ollamail-db apply -f deploy/helm/ci/postgres.yaml
kubectl create namespace ollamail
kubectl label namespace ollamail pod-security.kubernetes.io/enforce=restricted
kubectl -n ollamail create secret generic ollamail-secrets \
  --from-literal=OLLAMAIL_SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=OLLAMAIL_DATABASE_URL=postgresql+asyncpg://ollamail:ollamail-ci@postgres.ollamail-db:5432/ollamail
helm install ollamail deploy/helm/ollamail -n ollamail -f deploy/helm/ci/values-kind.yaml --wait
helm -n ollamail test ollamail
```
