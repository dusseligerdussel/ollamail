{{/* Chart name. */}}
{{- define "ollamail.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name; component suffixes are appended, so keep it short. */}}
{{- define "ollamail.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 40 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "ollamail.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ollamail.labels" -}}
helm.sh/chart: {{ include "ollamail.chart" . }}
{{ include "ollamail.selectorLabels" . }}
app.kubernetes.io/version: {{ include "ollamail.backendTag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: ollamail
{{- end }}

{{- define "ollamail.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ollamail.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Labels of one component: (dict "root" $ "component" "api") */}}
{{- define "ollamail.componentLabels" -}}
{{ include "ollamail.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "ollamail.componentSelectorLabels" -}}
{{ include "ollamail.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "ollamail.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ollamail.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* ------------------------------------------------------------------ images */}}

{{- define "ollamail.backendTag" -}}
{{- default .Chart.AppVersion .Values.image.backend.tag }}
{{- end }}

{{- define "ollamail.frontendTag" -}}
{{- default .Chart.AppVersion .Values.image.frontend.tag }}
{{- end }}

{{/* Pull policy for a tag: (dict "root" $ "tag" "1.2.3") */}}
{{- define "ollamail.pullPolicy" -}}
{{- if .root.Values.image.pullPolicy }}
{{- .root.Values.image.pullPolicy }}
{{- else if has .tag (list "latest" "edge") }}
{{- "Always" }}
{{- else }}
{{- "IfNotPresent" }}
{{- end }}
{{- end }}

{{- define "ollamail.backendImage" -}}
{{- $tag := include "ollamail.backendTag" . -}}
image: {{ printf "%s:%s" .Values.image.backend.repository $tag | quote }}
imagePullPolicy: {{ include "ollamail.pullPolicy" (dict "root" . "tag" $tag) }}
{{- end }}

{{- define "ollamail.frontendImage" -}}
{{- $tag := include "ollamail.frontendTag" . -}}
image: {{ printf "%s:%s" .Values.image.frontend.repository $tag | quote }}
imagePullPolicy: {{ include "ollamail.pullPolicy" (dict "root" . "tag" $tag) }}
{{- end }}

{{/* ------------------------------------------------------------ validation */}}

{{/* Settings that must come from a Secret, never from `config`. */}}
{{- define "ollamail.secretSettingPattern" -}}
^OLLAMAIL_(SECRET_KEY|SECRET_KEYS_OLD|SETUP_TOKEN|DATABASE_URL)$|(_SECRET|_PASSWORD|_API_KEY|_TOKEN)$
{{- end }}

{{/* Fails on invalid combinations; included once from every workload template. */}}
{{- define "ollamail.validate" -}}
{{- if not .Values.secrets.existingSecret }}
{{- fail "secrets.existingSecret is required: an existing Secret with at least OLLAMAIL_SECRET_KEY (see docs/operations/kubernetes.md)" }}
{{- end }}
{{- $sources := 0 }}
{{- if .Values.database.existingSecret }}{{ $sources = add1 $sources }}{{ end }}
{{- if .Values.database.cloudnativepg.cluster }}{{ $sources = add1 $sources }}{{ end }}
{{- if .Values.database.external.host }}{{ $sources = add1 $sources }}{{ end }}
{{- if gt $sources 1 }}
{{- fail "database: set only one of existingSecret, cloudnativepg.cluster and external.host" }}
{{- end }}
{{- if and .Values.database.external.host (not .Values.database.external.passwordSecret) }}
{{- fail "database.external.passwordSecret is required with database.external.host" }}
{{- end }}
{{- $pattern := include "ollamail.secretSettingPattern" . }}
{{- $configs := list .Values.config }}
{{- range $group := .Values.worker.groups }}
{{- $configs = append $configs (default dict $group.config) }}
{{- end }}
{{- range $config := $configs }}
{{- range $key, $_ := $config }}
{{- if regexMatch $pattern $key }}
{{- fail (printf "%s is a secret setting: put it into secrets.existingSecret instead of config" $key) }}
{{- end }}
{{- if eq $key "OLLAMAIL_DATA_DIR" }}
{{- fail "OLLAMAIL_DATA_DIR is managed by the chart (/data, see persistence)" }}
{{- end }}
{{- end }}
{{- end }}
{{- if not .Values.worker.groups }}
{{- fail "worker.groups must define at least one worker group" }}
{{- end }}
{{- $allQueues := list "sync" "llm" "tts" "ocr" "default" }}
{{- $consumed := list }}
{{- $names := list }}
{{- range $group := .Values.worker.groups }}
{{- if not $group.name }}
{{- fail "worker.groups: every group needs a name" }}
{{- end }}
{{- $name := toString $group.name }}
{{- if has $name $names }}
{{- fail (printf "worker.groups: duplicate name %q" $name) }}
{{- end }}
{{- $names = append $names $name }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,13}[a-z0-9])?$" $name) }}
{{- fail (printf "worker.groups: %q must be a lowercase DNS label of at most 15 characters" $name) }}
{{- end }}
{{- if not $group.queues }}
{{- fail (printf "worker.groups.%s.queues must not be empty" $name) }}
{{- end }}
{{- range $queue := $group.queues }}
{{- if not (has $queue $allQueues) }}
{{- fail (printf "worker.groups.%s.queues: unknown queue %q (sync, llm, tts, ocr, default)" $name $queue) }}
{{- end }}
{{- $consumed = append $consumed $queue }}
{{- end }}
{{- end }}
{{- range $queue := $allQueues }}
{{- if not (has $queue $consumed) }}
{{- fail (printf "worker.groups: no group consumes the queue %q, its jobs would never run" $queue) }}
{{- end }}
{{- end }}
{{- if ge (float64 .Values.worker.shutdownTimeout) (float64 .Values.worker.terminationGracePeriodSeconds) }}
{{- fail "worker.shutdownTimeout must be lower than worker.terminationGracePeriodSeconds" }}
{{- end }}
{{- if and .Values.ingress.enabled .Values.ingress.certManager.enabled (not .Values.ingress.certManager.issuerName) }}
{{- fail "ingress.certManager.issuerName is required with ingress.certManager.enabled" }}
{{- end }}
{{- end }}

{{/* ----------------------------------------------------------- backend env */}}

{{/* Setting value as string; maps and lists as JSON. */}}
{{- define "ollamail.envValue" -}}
{{- if or (kindIs "map" .) (kindIs "slice" .) }}
{{- toJson . }}
{{- else if kindIs "float64" . }}
{{- /* YAML numbers are float64; print integers without exponent. */}}
{{- if eq (float64 (int64 .)) . }}{{ int64 . }}{{ else }}{{ . }}{{ end }}
{{- else }}
{{- toString . }}
{{- end }}
{{- end }}

{{/*
envFrom and env of api, worker and the migration job.
(dict "root" $ "overrides" (dict "OLLAMAIL_WORKER_QUEUES" "llm"))
*/}}
{{- define "ollamail.backendEnv" -}}
{{- $root := .root -}}
{{- $values := $root.Values -}}
{{- $settings := dict "OLLAMAIL_DATA_DIR" "/data" -}}
{{- if and $values.ollama.enabled (not (hasKey $values.config "OLLAMAIL_LLM_BASE_URL")) }}
{{- $_ := set $settings "OLLAMAIL_LLM_BASE_URL" (printf "http://%s-ollama:%v" (include "ollamail.fullname" $root) $values.ollama.service.port) }}
{{- end }}
{{- $settings = merge (default dict .overrides) (deepCopy $values.config) $settings -}}
envFrom:
  - secretRef:
      name: {{ $values.secrets.existingSecret }}
  {{- with $values.extraEnvFrom }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
env:
  {{- range $key, $value := $settings }}
  {{- if not (kindIs "invalid" $value) }}
  - name: {{ $key }}
    value: {{ include "ollamail.envValue" $value | quote }}
  {{- end }}
  {{- end }}
  {{- with $values.database }}
  {{- if .existingSecret }}
  - name: OLLAMAIL_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ .existingSecret }}
        key: {{ .existingSecretKey }}
  {{- else if .cloudnativepg.cluster }}
  - name: OLLAMAIL_DB_USER
    valueFrom:
      secretKeyRef:
        name: {{ .cloudnativepg.cluster }}-app
        key: username
  - name: OLLAMAIL_DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: {{ .cloudnativepg.cluster }}-app
        key: password
  - name: OLLAMAIL_DB_NAME
    valueFrom:
      secretKeyRef:
        name: {{ .cloudnativepg.cluster }}-app
        key: dbname
  - name: OLLAMAIL_DATABASE_URL
    value: "postgresql+asyncpg://$(OLLAMAIL_DB_USER):$(OLLAMAIL_DB_PASSWORD)@{{ .cloudnativepg.cluster }}-rw:5432/$(OLLAMAIL_DB_NAME)"
  {{- else if .external.host }}
  - name: OLLAMAIL_DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: {{ .external.passwordSecret }}
        key: {{ .external.passwordSecretKey }}
  - name: OLLAMAIL_DATABASE_URL
    value: {{ printf "postgresql+asyncpg://%s:$(OLLAMAIL_DB_PASSWORD)@%s:%v/%s" .external.user .external.host .external.port .external.name | quote }}
  {{- end }}
  {{- end }}
  {{- with $values.extraEnv }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- end }}

{{/* Pod-level settings shared by all ollamail pods. */}}
{{- define "ollamail.podCommon" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Scheduling block: (dict "nodeSelector" .. "tolerations" .. "affinity" .. "topologySpreadConstraints" ..) */}}
{{- define "ollamail.scheduling" -}}
{{- with .nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/* Volumes of api and worker pods. */}}
{{- define "ollamail.backendVolumes" -}}
- name: tmp
  emptyDir: {}
- name: data
  {{- if .Values.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ default (printf "%s-data" (include "ollamail.fullname" .)) .Values.persistence.existingClaim }}
  {{- else }}
  emptyDir: {}
  {{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "ollamail.backendVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: data
  mountPath: /data
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end }}
