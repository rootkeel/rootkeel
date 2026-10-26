{{- /*
SPDX-License-Identifier: AGPL-3.0-only
*/ -}}

{{- define "rootkeel.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* The prefix of every resource name; short enough for the longest suffix, "-proxy-pods". */}}
{{- define "rootkeel.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 52 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "rootkeel.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "rootkeel.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels of one component: include "rootkeel.selector" (list . "broker") */}}
{{- define "rootkeel.selector" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/name: {{ include "rootkeel.name" $root }}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end }}

{{- define "rootkeel.tlsSecret" -}}
{{- .Values.tls.secretName | default (printf "%s-tls" (include "rootkeel.fullname" .)) }}
{{- end }}

{{- define "rootkeel.stateClaim" -}}
{{- .Values.persistence.existingClaim | default (printf "%s-state" (include "rootkeel.fullname" .)) }}
{{- end }}

{{/* Pod settings shared by every component. */}}
{{- define "rootkeel.podCommon" -}}
{{- with .Values.images.pullSecrets }}
imagePullSecrets:
  {{- range . }}
  - name: {{ . }}
  {{- end }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Replace text that must be present in a copied server configuration file, so that a change to
server/config fails the render instead of silently dropping a Kubernetes adjustment.
include "rootkeel.replace" (list text old new)
*/}}
{{- define "rootkeel.replace" -}}
{{- $text := index . 0 }}
{{- $old := index . 1 }}
{{- if not (contains $old $text) }}
{{- fail (printf "chart file no longer contains %q; update deploy/helm/rootkeel/templates/configmap.yaml" $old) }}
{{- end }}
{{- replace $old (index . 2) $text }}
{{- end }}

{{/*
The checks of rootkeel.config that values.schema.json cannot express, with the same rules, so a bad
value fails at install time rather than in a crash-looping broker.
*/}}
{{- define "rootkeel.validate" -}}
{{- $v := .Values }}
{{- $terminal := printf "owner.%s" $v.publicHost }}
{{- range $name, $host := dict "publicHost" $v.publicHost "relayHost" $v.relayHost }}
{{- if regexMatch "^[0-9.]+$" $host }}
{{- fail (printf "%s must be a DNS name, not an IP address (%q)" $name $host) }}
{{- end }}
{{- if or (hasSuffix ".rt.internal" $host) (eq $host "rt.internal") }}
{{- fail (printf "%s must not use the private relay suffix .rt.internal" $name) }}
{{- end }}
{{- end }}
{{- if eq $v.publicHost $v.relayHost }}
{{- fail "relayHost must differ from publicHost: the front door routes the agent tunnel by hostname" }}
{{- end }}
{{- if eq $v.relayHost $terminal }}
{{- fail (printf "relayHost must not be %s: that is the owner's terminal hostname" $terminal) }}
{{- end }}
{{- if gt (len $terminal) 253 }}
{{- fail (printf "publicHost is too long for the owner's terminal hostname %s" $terminal) }}
{{- end }}
{{- range $name, $limit := dict "grantIdleTimeout" $v.grantIdleTimeout "grantMaxLifetime" $v.grantMaxLifetime }}
{{- if and (gt (int $limit) 0) (lt (int $limit) 60) }}
{{- fail (printf "%s must be whole seconds, 0 (no limit, the default) or at least 60; got %d" $name (int64 $limit)) }}
{{- end }}
{{- end }}
{{- $mutable := list "latest" "stable" "main" "master" "edge" "nightly" "dev" "development" "staging" "stage" "prod" "production" "release" "current" "head" }}
{{- range $name := list "broker" "relay" "proxy" }}
{{- $ref := get $v.images $name }}
{{- $key := printf "images.%s" $name }}
{{- $rest := regexReplaceAll "@.*$" $ref "" }}
{{- $digest := ternary (regexReplaceAll "^[^@]*@" $ref "") "" (contains "@" $ref) }}
{{- $last := regexReplaceAll "^.*/" $rest "" }}
{{- $tag := ternary (regexReplaceAll "^[^:]*:" $last "") "" (contains ":" $last) }}
{{- if and $digest (not (regexMatch "^sha256:[a-f0-9]{64}$" $digest)) }}
{{- fail (printf "%s is not an image reference: %q" $key $ref) }}
{{- end }}
{{- if and $v.requireImageDigest (not $digest) }}
{{- fail (printf "%s must be pinned by digest (...@sha256:<64 hex>) because requireImageDigest is true; got %q" $key $ref) }}
{{- end }}
{{- if and (not $digest) (not $tag) }}
{{- fail (printf "%s has neither a tag nor a digest; pin one build, for example %s:1.2.3 or %s@sha256:<digest>" $key $rest $rest) }}
{{- end }}
{{- if and (not $digest) (has (lower $tag) $mutable) }}
{{- fail (printf "%s uses the moving tag %q; pin an immutable version tag or a digest" $key $tag) }}
{{- end }}
{{- end }}
{{- if and $v.tls.certManager.enabled (not $v.tls.certManager.issuerRef.name) }}
{{- fail "tls.certManager.issuerRef.name is required when tls.certManager.enabled is true" }}
{{- end }}
{{- end }}
