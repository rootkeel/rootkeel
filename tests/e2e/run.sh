#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# End-to-end test of a whole Rootkeel server on one Docker host, as CI runs it.
# Usage: sudo tests/e2e/run.sh
# Builds the broker, relay and agent images from this checkout, starts the Compose stack with a
# lab CA for term.example.com, owner.term.example.com and term-relay.example.com (names that
# resolve only inside the test's Docker network), and then, as the owner's computer would:
#   - rt login, after a wrong owner credential is refused;
#   - enrolls two named test machines (agent image) and two machines installed with
#     agent/install.sh on clean Debian 13 and Ubuntu 24.04 (install-check.sh);
#   - issues a grant on each machine and runs the browser flow: sign-in, a live shell over
#     WebSocket, foreign Origin refused (checks.py);
#   - runs rt attach in a real terminal (native_attach.py);
#   - revokes one machine: its open terminal closes and its grants end, the other's keep working;
#   - rt logout.
# Everything runs in containers; the host needs Docker, Python 3 and OpenSSL, and network access
# to fetch the images' and installer's pinned inputs. Everything it creates is removed afterwards,
# unless RT_E2E_KEEP=1. RT_E2E_HTTPS_BIND (default 127.0.0.1:18443) is where the host binds the
# stack's HTTPS port; nothing in the test uses it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run the end-to-end test as root (prepare.sh needs it)' >&2; exit 1; }
here=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/rootkeel-e2e.XXXXXX")
chmod 755 "$work"
tag=0.0.0-e2e
broker_image=rootkeel-e2e/broker:$tag
relay_image=rootkeel-e2e/relay:$tag
agent_image=rootkeel-e2e/agent:$tag
# Clean distribution images for the installer, pinned by digest.
debian_image=debian:13@sha256:4c25275446ca371766b8b093c696782eabce06263fccacb8d5d813b3dc44b308
ubuntu_image=ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
public=term.example.com
relay=term-relay.example.com
url=https://$public
env_file=$work/e2e.env
state=$work/state
client=rootkeel-e2e-client
machines=(m1 m2)
installed=(debian13 ubuntu2404)
containers=("$client")
volumes=()

# shellcheck source=scripts/lib/docker-plugins.sh
. "$here/scripts/lib/docker-plugins.sh"
ensure_docker_plugins "${RT_DOCKER_CONFIG_DIR:-$HOME/.cache/rootkeel/docker-config}"
export RT_DOCKER_CONFIG_DIR=${RT_DOCKER_CONFIG_DIR:-$HOME/.cache/rootkeel/docker-config}

step() { printf '\n== %s\n' "$*"; }
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }
compose() { "$here/scripts/compose.sh" "$env_file" "$@"; }
rt() { docker exec -i "$client" python3 /src/rt "$@"; }

cleanup() {
  local status=$?
  if [ "$status" != 0 ] && [ -f "$env_file" ]; then
    step "logs after a failure"
    compose logs --no-color --tail 200 >&2 || true
    for name in "${containers[@]}"; do
      echo "-- $name" >&2
      docker logs --tail 100 "$name" >&2 2>&1 || true
    done
  fi
  if [ "${RT_E2E_KEEP:-}" = 1 ]; then
    echo "Kept the stack and $work (RT_E2E_KEEP=1)" >&2
    return
  fi
  docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  if [ "${#volumes[@]}" -gt 0 ]; then docker volume rm "${volumes[@]}" >/dev/null 2>&1 || true; fi
  if [ -f "$env_file" ]; then compose down --volumes --remove-orphans >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

step "images"
docker buildx build --load -q -f "$here/images/broker.Dockerfile" --target runtime -t "$broker_image" "$here"
docker buildx build --load -q -f "$here/images/relay.Dockerfile" -t "$relay_image" "$here"
docker buildx build --load -q -f "$here/images/agent.Dockerfile" -t "$agent_image" "$here"
docker pull -q "$debian_image" >/dev/null
docker pull -q "$ubuntu_image" >/dev/null

step "server: prepare.sh --lab-ca, then compose.sh up"
cat > "$env_file" <<ENV
RT_ENVIRONMENT=e2e
RT_PUBLIC_HOST=$public
RT_RELAY_HOST=$relay
RT_PUBLIC_PORT=443
RT_HTTPS_BIND=${RT_E2E_HTTPS_BIND:-127.0.0.1:18443}
RT_STATE_DIR=$state
RT_HSTS_MAX_AGE=300
RT_REQUIRE_IMAGE_DIGEST=false
RT_BROKER_IMAGE=$broker_image
RT_RELAY_IMAGE=$relay_image
$(grep '^RT_PROXY_IMAGE=' "$here/server/example.env")
ENV
"$here/scripts/prepare.sh" "$env_file" --lab-ca
compose up -d --quiet-pull
proxy=$(docker inspect -f '{{(index .NetworkSettings.Networks "rootkeel-e2e_edge").IPAddress}}' "$(compose ps -q proxy)")
[ -n "$proxy" ] || fail "the proxy has no address on the edge network"
network=(--network rootkeel-e2e_edge --add-host "$public:$proxy" --add-host "owner.$public:$proxy" --add-host "$relay:$proxy")
install -m 644 "$state/certs/ca.crt" "$work/ca.crt"

# The owner's computer: rt and its checks as an ordinary user, with the lab CA.
docker run -d --init --name "$client" "${network[@]}" -u 1000:1000 -e HOME=/home/owner -e RT_CA_CERT=/ca/ca.crt \
  -v "$here:/src:ro" -v "$work/ca.crt:/ca/ca.crt:ro" --entrypoint sleep "$agent_image" infinity >/dev/null
ready=
for _ in $(seq 60); do
  if docker exec "$client" python3 -c 'import ssl, sys, urllib.request
urllib.request.urlopen(sys.argv[1], context=ssl.create_default_context(cafile="/ca/ca.crt"), timeout=5)' "$url/" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[ -n "$ready" ] || fail "the stack does not answer on $url"
pass "the stack answers on $url with the lab CA's certificate"

step "rt login"
if printf '%064d\n' 0 | rt login --url "$url"; then fail "rt login accepted a wrong owner credential"; fi
docker exec "$client" test ! -e "/home/owner/.config/rt/$public/credential" || fail "a refused login saved a credential"
pass "a wrong owner credential is refused and nothing is saved"
rt login --url "$url" < "$state/owner"
[ "$(docker exec "$client" stat -c %a "/home/owner/.config/rt/$public/credential")" = 600 ] \
  || fail "the saved owner credential is not mode 600"
pass "rt login saved the owner credential, readable by the owner only"

step "enroll machines"
enroll() { # name
  local code
  code=$(rt enrollment --name "$1" | sed -n 's/^Enrollment code.*: //p')
  [ -n "$code" ] || fail "rt enrollment printed no code"
  printf '%s\n' "$code" | rt enroll "/home/owner/$1.json" >/dev/null 2>&1
  docker exec "$client" cat "/home/owner/$1.json" > "$work/$1.json"
  chmod 644 "$work/$1.json"
  pass "rt enrollment --name $1 and rt enroll wrote its machine credential"
}
for name in "${machines[@]}" "${installed[@]}"; do enroll "$name"; done
# Test machines from the agent image: the agent, and the owner's shell in a second container
# that shares its network and the helper socket, as the two systemd units do on a real machine.
for name in "${machines[@]}"; do
  install -m 600 -o 1001 -g 1001 "$work/$name.json" "$work/$name.agent.json"
  volumes+=("rootkeel-e2e-$name-helper")
  containers+=("rootkeel-e2e-$name" "rootkeel-e2e-$name-terminal")
  docker run -d --init --name "rootkeel-e2e-$name" "${network[@]}" \
    -e RT_MACHINE_FILE=/credentials/machine.json -e RT_FRPC_CONFIG=/var/lib/rootkeel/frpc.toml \
    -v "rootkeel-e2e-$name-helper:/run/rootkeel-helper" -v "$work/$name.agent.json:/credentials/machine.json:ro" \
    -v "$work/ca.crt:/ca/ca.crt:ro" "$agent_image" >/dev/null
  docker run -d --init --name "rootkeel-e2e-$name-terminal" --network "container:rootkeel-e2e-$name" -u 1000:1000 \
    -e HOME=/home/owner -e USER=owner -e RT_SESSION=main -e E2E_MACHINE="$name" -w /home/owner \
    -v "rootkeel-e2e-$name-helper:/run/rootkeel-helper" --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint python3 "$agent_image" -m rootkeel.terminal >/dev/null
done
# Machines installed by agent/install.sh on clean distributions, in parallel.
for name in "${installed[@]}"; do
  case $name in debian13) image=$debian_image ;; ubuntu2404) image=$ubuntu_image ;; esac
  containers+=("rootkeel-e2e-$name")
  docker run -d --init --name "rootkeel-e2e-$name" "${network[@]}" -v "$here:/src:ro" \
    -v "$work/$name.json:/machine.json:ro" -v "$work/ca.crt:/ca/ca.crt:ro" \
    "$image" bash /src/tests/e2e/install-check.sh "$name" >/dev/null
done
for name in "${installed[@]}"; do
  for _ in $(seq 600); do
    if docker logs "rootkeel-e2e-$name" 2>&1 | grep -q '^INSTALL-CHECK-READY$'; then break; fi
    [ "$(docker inspect -f '{{.State.Running}}' "rootkeel-e2e-$name")" = true ] || break
    sleep 1
  done
  docker logs "rootkeel-e2e-$name" 2>&1 | grep -E '^(PASS|FAIL) ' || true
  docker logs "rootkeel-e2e-$name" 2>&1 | grep -q '^INSTALL-CHECK-READY$' || fail "install check on $name"
done
rt machines
docker exec "$client" python3 /src/tests/e2e/checks.py online "$url" /ca/ca.crt "${machines[@]}" "${installed[@]}"

step "grants and the browser"
if rt grant main 2>"$work/err"; then fail "rt grant without --machine was accepted with several machines"; fi
grep -q 'Several machines are enrolled' "$work/err" || fail "rt grant without --machine: $(cat "$work/err")"
pass "with several machines, a grant must name one"
if rt grant nosuch --machine m1 2>"$work/err"; then fail "a grant for an unknown session was issued"; fi
grep -q 'No session nosuch' "$work/err" || fail "rt grant nosuch: $(cat "$work/err")"
pass "a grant for an unknown session is refused"
for name in "${machines[@]}" "${installed[@]}"; do
  rt grant main --machine "$name" > "$work/grant-$name.json"
  docker cp "$work/grant-$name.json" "$client:/tmp/grant-$name.json"
  docker exec "$client" python3 /src/tests/e2e/checks.py browser "/tmp/grant-$name.json" /ca/ca.crt "$name"
done
rt grants

step "rt attach"
python3 "$here/tests/e2e/native_attach.py" "$client" "$(compose ps -q broker)" m1 m2
rt grant build --machine m2 > "$work/grant-build.json"
pass "the session rt attach --create started is there for a new grant"
rt revoke "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["id"])' "$work/grant-build.json")"
rt grants | grep -q build && fail "rt revoke left the grant open"
pass "rt revoke ends a grant"

step "machine revoke"
docker exec "$client" python3 /src/tests/e2e/checks.py revoke /tmp/grant-m1.json /ca/ca.crt m1 /tmp/grant-m2.json m2
rt machines | grep -E '^m_[0-9a-f]+ +m1 +revoked' >/dev/null || fail "rt machines does not show m1 revoked"
open_grants=$(rt grants)
if printf '%s\n' "$open_grants" | grep -qw m1; then fail "m1 still has open grants"; fi
printf '%s\n' "$open_grants" | grep -qw m2 || fail "m2 lost its grant"
pass "only the revoked machine's grants ended"
compose exec -T broker python3 -m rootkeel.admin machines

step "rt logout"
rt logout
docker exec "$client" test ! -e "/home/owner/.config/rt/$public/credential" || fail "rt logout left the credential"
pass "rt logout deleted the saved owner credential"

printf '\nE2E-PASS\n'
