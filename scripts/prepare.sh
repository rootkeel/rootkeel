#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Prepare one environment's private state on its server. Does not start services.
# Usage: scripts/prepare.sh <env-file> [--lab-ca]
# Idempotent: never replaces an existing owner credential, database or certificate.
# Without --lab-ca, the TLS certificate/key named by the env file (default
# <state>/certs/server.{crt,key}) must already exist and cover both hostnames.
# --lab-ca generates a private CA and server certificate for this environment only;
# use it for labs, never for a public deployment.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run preparation as root on the server' >&2; exit 1; }
here=$(cd "$(dirname "$0")/.." && pwd)
[ $# -ge 1 ] || { echo 'usage: scripts/prepare.sh <env-file> [--lab-ca]' >&2; exit 64; }
env_file=$(realpath "$1")
lab_ca=${2:-}
case "$lab_ca" in '' | --lab-ca) ;; *) echo 'Expected optional --lab-ca' >&2; exit 64 ;; esac
setting() { PYTHONPATH="$here" python3 -m rootkeel.config get "$env_file" "$1"; }
PYTHONPATH="$here" python3 -m rootkeel.config check-env "$env_file"
environment=$(setting RT_ENVIRONMENT)
public=$(setting RT_PUBLIC_HOST)
relay=$(setting RT_RELAY_HOST)
state=$(setting RT_STATE_DIR)
mkdir -p "$state"
state=$(realpath "$state")
case "$state/" in "$here/"*) echo 'RT_STATE_DIR must be outside the checkout' >&2; exit 1 ;; esac
umask 077
mkdir -p "$state/certs" "$state/db"
chmod 700 "$state" "$state/db"
chown 65534:65534 "$state/db"
if [ ! -s "$state/owner" ]; then
  openssl rand -hex 32 > "$state/owner"
fi
chmod 600 "$state/owner"
chown 65534:65534 "$state/owner"
if [ ! -s "$state/db-key" ]; then
  openssl rand -hex 32 > "$state/db-key"
fi
chmod 600 "$state/db-key"
chown 65534:65534 "$state/db-key"
cert=$(setting RT_TLS_CERT_FILE)
key=$(setting RT_TLS_KEY_FILE)
cert=${cert:-$state/certs/server.crt}
key=${key:-$state/certs/server.key}
if [ "$lab_ca" = --lab-ca ] && [ ! -s "$cert" ]; then
  if [ "$cert" != "$state/certs/server.crt" ] || [ "$key" != "$state/certs/server.key" ]; then
    echo '--lab-ca writes only <state>/certs; unset RT_TLS_CERT_FILE/RT_TLS_KEY_FILE' >&2
    exit 1
  fi
  (
    cd "$state/certs"
    openssl req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes \
      -subj "/CN=rootkeel $environment lab CA" \
      -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
      -keyout ca.key -out ca.crt >/dev/null 2>&1
    openssl req -newkey rsa:3072 -sha256 -nodes -subj "/CN=$public" \
      -keyout server.key -out server.csr >/dev/null 2>&1
    printf 'subjectAltName=DNS:%s,DNS:*.%s,DNS:%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
      "$public" "$public" "$relay" > ext.cnf
    openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
      -days 30 -sha256 -extfile ext.cnf -out server.crt >/dev/null 2>&1
    openssl x509 -in server.crt -pubkey -noout | openssl pkey -pubin -outform der \
      | openssl dgst -sha256 -binary | openssl base64 -A > server.spki
    rm -f server.csr ext.cnf ca.srl
    chmod 644 ca.crt server.crt server.spki
    chmod 600 ca.key server.key
  )
fi
if [ ! -s "$cert" ] || [ ! -s "$key" ]; then
  echo "TLS certificate/key missing: $cert / $key. Install a certificate for $public, owner.$public and $relay, or use --lab-ca in a lab." >&2
  exit 1
fi
# The owner's terminal host is owner.<public host>.
for host in "$public" "owner.$public" "$relay"; do
  openssl x509 -in "$cert" -noout -checkhost "$host" | grep -q 'does match' \
    || { echo "TLS certificate $cert does not cover $host" >&2; exit 1; }
done
[ "$(openssl x509 -in "$cert" -pubkey -noout)" = "$(openssl pkey -in "$key" -pubout)" ] \
  || { echo "TLS key $key does not match certificate $cert" >&2; exit 1; }
echo "Environment $environment state ready in $state; no credentials printed."
echo "The owner credential is in $state/owner: give it to rt login on your computer (sudo cat $state/owner)."
