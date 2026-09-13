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
{{- if .Values.auth.azureAd.enabled -}}
{{- if not .Values.auth.azureAd.tenantId -}}
{{- fail "auth.azureAd.enabled=true requires auth.azureAd.tenantId." -}}
{{- end -}}
{{- if not .Values.auth.azureAd.clientId -}}
{{- fail "auth.azureAd.enabled=true requires auth.azureAd.clientId." -}}
{{- end -}}
{{- if not .Values.auth.azureAd.redirectUri -}}
{{- fail "auth.azureAd.enabled=true requires auth.azureAd.redirectUri (must exactly match the Redirect URI registered on the Azure AD App Registration)." -}}
{{- end -}}
{{- if and (not .Values.auth.azureAd.clientSecret) (not .Values.auth.azureAd.existingSecret) -}}
{{- fail "auth.azureAd.enabled=true requires either auth.azureAd.clientSecret or auth.azureAd.existingSecret." -}}
{{- end -}}
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

{{/*
The Secret name/key holding the Azure AD client secret, whichever path
produced it. Only meaningful when auth.azureAd.enabled - see
agenthive.validate.
*/}}
{{- define "agenthive.azureAdSecretName" -}}
{{- if .Values.auth.azureAd.existingSecret -}}
{{ .Values.auth.azureAd.existingSecret }}
{{- else -}}
{{ include "agenthive.fullname" . }}-azure-ad
{{- end -}}
{{- end -}}

{{- define "agenthive.azureAdSecretKey" -}}
{{- if .Values.auth.azureAd.existingSecret -}}
{{ .Values.auth.azureAd.existingSecretKey }}
{{- else -}}
client-secret
{{- end -}}
{{- end -}}

{{/*
Full image reference: registry/repository, then either @digest (pinned) or
:tag (defaulting to the chart's appVersion, since the image and chart are
built from the same release - see .github/workflows/release.yml).
*/}}
{{- define "agenthive.image" -}}
{{- $repo := printf "%s/%s" .Values.image.registry .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $repo .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{/*
--- Standalone review-UI deployment (see values.yaml's ui.* and
templates/ui-*.yaml). Deliberately its own app.kubernetes.io/name so its
selector never collides with the main Deployment/Service above - changing
those two chart resources' existing selectors on upgrade is not allowed
by Kubernetes, so this stays fully separate rather than reusing them.
*/}}
{{- define "agenthive.ui.fullname" -}}
{{ include "agenthive.fullname" . }}-ui
{{- end -}}

{{- define "agenthive.ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agenthive.name" . }}-ui
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agenthive.ui.labels" -}}
helm.sh/chart: {{ include "agenthive.chart" . }}
{{ include "agenthive.ui.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "agenthive.ui.image" -}}
{{- $repo := printf "%s/%s" .Values.ui.image.registry .Values.ui.image.repository -}}
{{- if .Values.ui.image.digest -}}
{{- printf "%s@%s" $repo .Values.ui.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (.Values.ui.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}
