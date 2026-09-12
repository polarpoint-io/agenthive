{{/*
Expand the name of the chart.
*/}}
{{- define "agenthive.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name.
*/}}
{{- define "agenthive.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agenthive.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agenthive.labels" -}}
helm.sh/chart: {{ include "agenthive.chart" . }}
{{ include "agenthive.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "agenthive.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agenthive.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agenthive.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "agenthive.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Guardrails - fail the render early rather than produce a Deployment that
would silently corrupt data or crash-loop:
  - SQLite + more than one replica: a single file, ReadWriteOnce PVC, one
    writer. Multiple replicas would either fail to mount (RWO on
    different nodes) or race on the same file.
  - Postgres selected but no way to get a DATABASE_URL.
*/}}
{{- define "agenthive.validate" -}}
{{- if and (eq .Values.database.type "sqlite") (gt (int .Values.replicaCount) 1) -}}
{{- fail "database.type=sqlite supports replicaCount=1 only (one writer, one ReadWriteOnce volume). Set database.type=postgres to run more than one replica." -}}
{{- end -}}
{{- if eq .Values.database.type "postgres" -}}
{{- if and (not .Values.database.postgres.url) (not .Values.database.postgres.existingSecret) -}}
{{- fail "database.type=postgres requires either database.postgres.url or database.postgres.existingSecret to be set." -}}
{{- end -}}
{{- end -}}
{{- if and .Values.tls.enabled (not .Values.tls.secretName) -}}
{{- fail "tls.enabled=true requires tls.secretName (a kubernetes.io/tls Secret with tls.crt/tls.key)." -}}
{{- end -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled (not .Values.ingress.tls.secretName) -}}
{{- fail "ingress.tls.enabled=true requires ingress.tls.secretName." -}}
{{- end -}}
{{- end -}}

{{/*
The Secret name holding DATABASE_URL, whichever path produced it.
*/}}
{{- define "agenthive.databaseSecretName" -}}
{{- if .Values.database.postgres.existingSecret -}}
{{ .Values.database.postgres.existingSecret }}
{{- else -}}
{{ include "agenthive.fullname" . }}-database
{{- end -}}
{{- end -}}

{{- define "agenthive.databaseSecretKey" -}}
{{- if .Values.database.postgres.existingSecret -}}
{{ .Values.database.postgres.existingSecretKey }}
{{- else -}}
database-url
{{- end -}}
{{- end -}}

{{/*
The Secret name holding REDIS_URL, if redis is configured at all.
*/}}
{{- define "agenthive.redisConfigured" -}}
{{- if or .Values.redis.url .Values.redis.existingSecret -}}true{{- end -}}
{{- end -}}

{{- define "agenthive.redisSecretName" -}}
{{- if .Values.redis.existingSecret -}}
{{ .Values.redis.existingSecret }}
{{- else -}}
{{ include "agenthive.fullname" . }}-redis
{{- end -}}
{{- end -}}

{{- define "agenthive.redisSecretKey" -}}
{{- if .Values.redis.existingSecret -}}
{{ .Values.redis.existingSecretKey }}
{{- else -}}
redis-url
{{- end -}}
{{- end -}}

{{- define "agenthive.scheme" -}}
{{- if .Values.tls.enabled -}}https{{- else -}}http{{- end -}}
{{- end -}}
