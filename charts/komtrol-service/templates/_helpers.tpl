{{/*
Name helpers.

The chart is generic, so the application name comes from the release name, not
from the chart name. The ApplicationSet sets releaseName to the values file's
basename, which makes the chain explicit:

  values/production/customer-a/komtrol-api.yaml
    -> release name  komtrol-api
    -> every object  komtrol-api

Do not fall back to .Chart.Name here. Two services rendered from one generic
chart into one namespace would then share a name and a selector, and the
Service for one would route to the pods of the other.

The fullname is also the ServiceAccount name, and the Pod Identity association
the platform team creates is bound to that exact string. Renaming a release
therefore means asking for a new association.
*/}}

{{- define "komtrol-service.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "komtrol-service.fullname" -}}
{{- default (include "komtrol-service.name" .) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "komtrol-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 -}}
{{- end -}}

{{/*
Selector labels. These go into the Deployment selector, which is immutable
after creation, so nothing volatile may appear here. In particular the chart
version must not.
*/}}
{{- define "komtrol-service.selectorLabels" -}}
app: {{ include "komtrol-service.name" . }}
app.kubernetes.io/name: {{ include "komtrol-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "komtrol-service.labels" -}}
helm.sh/chart: {{ include "komtrol-service.chart" . }}
{{ include "komtrol-service.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
The image reference.

Both halves are required, and each is required from a different file, which is
what keeps the two layers of the values tree honest:

  repository  values/common/<application>.yaml   one line, rarely changes
  digest      values/<env>/<tenant>/<app>.yaml   written by CI on every build

Digest only, never a tag: a tag can be moved to point at different content, and
then the cluster and the repository no longer agree on what is running. An
empty digest fails the render, which stops an accidental deployment of whatever
latest happens to be.
*/}}
{{- define "komtrol-service.image" -}}
{{- $repo := required "image.repository is required. Set it in values/common/<application>.yaml" .Values.image.repository -}}
{{- $digest := required "image.digest is required. CI writes it into the tenant values file, see runbook 5.6" .Values.image.digest -}}
{{- printf "%s@%s" $repo $digest -}}
{{- end -}}

{{/*
Probe headers, rendered into every probe or omitted entirely. Values are
literals: there is no secretKeyRef and no variable expansion here, so a
credential written into a header ends up committed to this repository.
*/}}
{{- define "komtrol-service.probeHeaders" -}}
{{- with .Values.probes.headers }}
httpHeaders: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
