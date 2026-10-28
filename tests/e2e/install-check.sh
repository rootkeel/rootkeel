#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# The agent installer on a clean distribution, then the installed agent connected to the server.
# Runs as root in a fresh debian:13 or ubuntu:24.04 container that run.sh starts, with the checkout
# at /src (read-only), an enrolled machine credential at /machine.json and the lab CA it names at
# /ca/ca.crt.
# Usage: install-check.sh <machine-name> [--no-connect]
# Installs with agent/install.sh --no-start (there is no systemd in the container), checks the
# pinned binaries, the credential's protection and the generated units, then, unless --no-connect,
# starts both units' commands as their own accounts, prints INSTALL-CHECK-READY and keeps running.
set -euo pipefail
name=${1:?machine name required}
connect=${2:-}
distribution=$(sed -n 's/^PRETTY_NAME="\(.*\)"$/\1/p' /etc/os-release)
fail() { echo "FAIL $distribution: $*" >&2; exit 1; }
pass() { echo "PASS $distribution: $*"; }

useradd --create-home --shell /bin/bash alice
# The credential as rt enroll leaves it in the owner's home.
install -m 600 -o alice -g alice /machine.json /home/alice/machine.json
# Root's own shell is shared only on request.
if /src/agent/install.sh root /home/alice/machine.json --no-start > /tmp/install.log 2>&1; then
  fail 'the installer shared root without --allow-root'
fi
grep -q -- 'pass --allow-root to share root' /tmp/install.log || fail "root refusal: $(cat /tmp/install.log)"
pass 'the installer refuses root unless --allow-root is given'
/src/agent/install.sh alice /home/alice/machine.json --no-start > /tmp/install.log 2>&1 \
  || { cat /tmp/install.log >&2; fail 'agent/install.sh failed'; }
tail -n 1 /tmp/install.log
[ "$(zellij --version)" = 'zellij 0.45.1' ] || fail "zellij is $(zellij --version)"
[ "$(frpc --version)" = 0.71.0 ] || fail "frpc is $(frpc --version)"
pass 'installer put checksum-verified Zellij 0.45.1 and frpc 0.71.0 in place'

state=/var/lib/rootkeel
[ "$(stat -c %U:%a "$state/machine.json")" = rt-agent:600 ] || fail 'machine.json is not rt-agent:600'
[ "$(stat -c %U:%a "$state/ca.crt")" = rt-agent:600 ] || fail 'the CA copy is not rt-agent:600'
[ "$(stat -c %U:%G:%a /home/alice/machine.json)" = root:root:600 ] || fail 'the enrollment copy was not taken from the owner'
for file in "$state/machine.json" /home/alice/machine.json; do
  if runuser -u alice -- cat "$file" >/dev/null 2>&1; then fail "the shell's account can read $file"; fi
done
[ "$(id -u rt-agent)" != 0 ] || fail 'rt-agent is root'
if id -nG rt-agent | grep -qw alice; then fail "rt-agent is in the shell account's group"; fi
python3 -c 'import json, sys; c = json.load(open(sys.argv[1])); sys.exit(c["ca_file"] != "/var/lib/rootkeel/ca.crt")' \
  "$state/machine.json" || fail 'machine.json does not name the agent copy of the CA'
pass 'machine credential and CA are readable by rt-agent only, not by the shell account'

apt-get install -y --no-install-recommends systemd >/dev/null 2>&1 || fail 'cannot install systemd-analyze'
systemd-analyze verify /etc/systemd/system/rootkeel-agent.service /etc/systemd/system/rootkeel-terminal.service \
  || fail 'systemd-analyze rejects the generated units'
pass 'the generated rootkeel-terminal and rootkeel-agent units verify'

[ "$connect" = --no-connect ] && exit 0
# Each unit's command as systemd would run it: its account, working directory and environment.
start_unit() { # unit account [extra environment...]
  local unit=/etc/systemd/system/$1.service account=$2 directory command
  shift 2
  local -a environment
  directory=$(sed -n 's/^WorkingDirectory=//p' "$unit")
  command=$(sed -n 's/^ExecStart=//p' "$unit")
  mapfile -t environment < <(sed -n 's/^Environment=//p' "$unit")
  # shellcheck disable=SC2086 # the unit's command line, split as systemd splits it
  (cd "$directory" && exec runuser -u "$account" -- env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    "${environment[@]}" "$@" $command) &
}
start_unit rootkeel-terminal alice "E2E_MACHINE=$name"
start_unit rootkeel-agent rt-agent
echo INSTALL-CHECK-READY
wait -n
fail 'an installed unit exited'
