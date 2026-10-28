#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Install the agent on the Linux machine to share, from a checkout of the release you run.
# Usage: sudo agent/install.sh <ordinary-user> <machine.json> [--no-start] [--allow-root]
# <ordinary-user> is the account whose shell is shared; machine.json comes from rt enroll.
# --allow-root shares root's own shell instead, with full control of the machine.
# The session is named main unless RT_SESSION names another.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo 'Run installer as root' >&2; exit 1; }
owner=${1:?ordinary Linux user required}
source_credential=${2:?enrolled machine.json required}
start_mode=start
allow_root=no
for option in "${@:3}"; do
  case "$option" in
    --no-start) start_mode=--no-start ;;
    --allow-root) allow_root=yes ;;
    *) echo 'Expected optional --no-start or --allow-root' >&2; exit 1 ;;
  esac
done
owner_uid=$(id -u "$owner")
[ "$owner_uid" -ne 0 ] || [ "$allow_root" = yes ] \
  || { echo 'Shell must run as an ordinary user; pass --allow-root to share root' >&2; exit 1; }
# Dedicated account never joins the shell user's groups.
if ! id rt-agent >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/rootkeel --shell /usr/sbin/nologin rt-agent
fi
[ "$(id -u rt-agent)" != "$owner_uid" ] || { echo 'Choose a shell user other than rt-agent' >&2; exit 1; }
[ "$(id -u rt-agent)" -ne 0 ] || { echo 'rt-agent must not be root' >&2; exit 1; }
owner_group=$(id -gn "$owner")
owner_home=$(getent passwd "$owner" | cut -d: -f6)
project=$(cd "$(dirname "$0")/.." && pwd)
session=${RT_SESSION:-main}
[[ "$session" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || { echo 'RT_SESSION must be letters, digits, - and _' >&2; exit 1; }
case "$(uname -m)" in
  x86_64)
    zarch=x86_64; farch=amd64
    zsum=40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc
    fsum=84f27e39f11169f7adcef8e8b70c9329de17747b1f14dad9fb95eef5682ea716 ;;
  aarch64)
    zarch=aarch64; farch=arm64
    zsum=05f0802afadd53f8db9514e7cae53c9ae8432fed1b35b8294aa816ee3044a16b
    fsum=f33c293c275d8fc68c654b6fba8f10b2551d6463d09a9fc9cffb7227eae82266 ;;
  *) echo 'Unsupported Linux architecture' >&2; exit 1 ;;
esac
# Ubuntu 24.04 / Debian 13. Distribution security updates supply Python/aiohttp;
# Zellij and frp remain exact checksum-verified upstream builds.
apt-get update
apt-get install -y --no-install-recommends python3 python3-aiohttp ca-certificates curl
# Snapshot and protect the same inode, even when the input lives in a directory
# writable by the shell. Never follow a substituted symlink as root.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
python3 - "$source_credential" "$scratch/machine.json" <<'PYINPUT'
import json, os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as source:
    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
        sys.exit("Credential must be a regular file")
    data = source.read(65537)
    if len(data) > 65536:
        sys.exit("Credential file is too large")
    config = json.loads(data)
    if not isinstance(config, dict) or not all(isinstance(config.get(k), str) and config[k]
                                               for k in ('id', 'credential', 'relay_host', 'domain')):
        sys.exit("Incomplete machine.json")
    os.fchown(source.fileno(), 0, 0)
    os.fchmod(source.fileno(), 0o600)
with open(sys.argv[2], 'wb') as target:
    target.write(data)
os.chmod(sys.argv[2], 0o600)
PYINPUT
source_credential="$scratch/machine.json"
curl -fsSLo "$scratch/zellij.tgz" "https://github.com/zellij-org/zellij/releases/download/v0.45.1/zellij-$zarch-unknown-linux-musl.tar.gz"
printf '%s  %s\n' "$zsum" "$scratch/zellij.tgz" | sha256sum -c -
curl -fsSLo "$scratch/frp.tgz" "https://github.com/fatedier/frp/releases/download/v0.71.0/frp_0.71.0_linux_$farch.tar.gz"
printf '%s  %s\n' "$fsum" "$scratch/frp.tgz" | sha256sum -c -
tar -xzf "$scratch/zellij.tgz" -C "$scratch" zellij
tar -xzf "$scratch/frp.tgz" -C "$scratch" --strip-components=1 "frp_0.71.0_linux_$farch/frpc" "frp_0.71.0_linux_$farch/LICENSE"
install -m755 "$scratch/zellij" "$scratch/frpc" /usr/local/bin/
install -d -m755 /opt/rootkeel /usr/share/doc/rootkeel
rm -rf /opt/rootkeel/rootkeel /opt/rootkeel/agent
cp -R "$project/rootkeel" "$project/agent" /opt/rootkeel/
# Readable by the agent and the shared account whatever the checkout's umask was.
chmod -R u=rwX,go=rX /opt/rootkeel
install -m644 "$project/notices/Zellij-MIT.txt" /usr/share/doc/rootkeel/zellij-LICENSE.md
install -m644 "$scratch/LICENSE" /usr/share/doc/rootkeel/frp-LICENSE
install -m644 "$project/LICENSE" "$project/THIRD_PARTY_NOTICES.md" /usr/share/doc/rootkeel/
install -m755 "$project/rt" /usr/local/bin/rt
# An upgrade must retire the old same-UID agent before changing its unit.
if [ "$start_mode" = start ] && systemctl cat rootkeel-agent.service >/dev/null 2>&1; then
  systemctl stop rootkeel-agent.service
fi
install -d -m700 -o rt-agent -g rt-agent /var/lib/rootkeel
# Existing state, including the generated frpc config, stays the agent's alone.
python3 - <<'PYSTATE'
import os, pwd, stat
from pathlib import Path
account = pwd.getpwnam("rt-agent")
for name in ("machine.json", "frpc.toml", "ca.crt", "installed-packages.txt"):
    path = Path('/var/lib/rootkeel') / name
    if not path.exists() and not path.is_symlink():
        continue
    if not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit("Agent state must contain regular files: " + name)
    os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    path.chmod(0o600)
PYSTATE
install -m600 -o rt-agent -g rt-agent "$source_credential" /var/lib/rootkeel/machine.json
# Preserve private-CA enrollments when the CA originally lived in the user's
# home: ProtectHome and the new UID intentionally cannot read that directory.
python3 - /var/lib/rootkeel/machine.json <<'PYCA'
import json, os, pwd, shutil, sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text())
if config.get("ca_file"):
    source = Path(config["ca_file"]).resolve()
    target = path.parent / "ca.crt"
    if source != target:
        shutil.copyfile(source, target)
    account = pwd.getpwnam("rt-agent")
    os.chown(target, account.pw_uid, account.pw_gid)
    target.chmod(0o600)
    config["ca_file"] = str(target)
    path.write_text(json.dumps(config))
PYCA
# The owner can create the helper socket; only the agent group can connect.
install -d -m2710 -o "$owner" -g rt-agent /var/lib/rootkeel-helper
# Restrict ptrace for unprivileged processes; also keep secrets out of core dumps.
install -d -m755 /etc/sysctl.d
ptrace_scope=1
if [ -r /proc/sys/kernel/yama/ptrace_scope ]; then
  ptrace_scope=$(cat /proc/sys/kernel/yama/ptrace_scope)
fi
[ "$ptrace_scope" -ge 1 ] || ptrace_scope=1
printf 'kernel.yama.ptrace_scope = %s\n' "$ptrace_scope" > /etc/sysctl.d/60-rootkeel.conf
if [ "$start_mode" = start ]; then
  sysctl -w "kernel.yama.ptrace_scope=$ptrace_scope" >/dev/null
fi
# Verify the enrollment target without printing the credential.
(cd /opt/rootkeel && python3 -c 'import json, sys; from rootkeel.agent import frpc_config
frpc_config(json.load(open(sys.argv[1])))' /var/lib/rootkeel/machine.json)
# A shared root shell keeps root's privileges; an ordinary user's gains none.
terminal_limits=$'NoNewPrivileges=true\nCapabilityBoundingSet='
[ "$owner_uid" -ne 0 ] || terminal_limits='# Shared as root (--allow-root): no privilege limits.'
cat > /etc/systemd/system/rootkeel-terminal.service <<UNIT
[Unit]
Description=Rootkeel terminal: the owner's Zellij session and token helper
After=network-online.target
[Service]
Type=simple
User=$owner
Group=$owner_group
WorkingDirectory=$owner_home
Environment=HOME=$owner_home
Environment=USER=$owner
Environment=PYTHONPATH=/opt/rootkeel
Environment=RT_SESSION=$session
Environment=RT_TERMINAL_SOCKET=/var/lib/rootkeel-helper/terminal.sock
Environment=TERM=xterm-256color
Environment=SHELL=/bin/bash
ExecStart=/usr/bin/python3 -m rootkeel.terminal
Restart=on-failure
RestartSec=3
KillMode=process
UMask=0077
$terminal_limits
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/rootkeel-agent.service <<UNIT
[Unit]
Description=Rootkeel agent: outbound tunnel to the relay
After=network-online.target rootkeel-terminal.service
Wants=network-online.target rootkeel-terminal.service
[Service]
Type=simple
User=rt-agent
Group=rt-agent
WorkingDirectory=/opt/rootkeel
Environment=HOME=/var/lib/rootkeel
Environment=RT_MACHINE_FILE=/var/lib/rootkeel/machine.json
Environment=RT_SESSION=$session
Environment=RT_TERMINAL_SOCKET=/var/lib/rootkeel-helper/terminal.sock
Environment=TERM=xterm-256color
Environment=SHELL=/bin/bash
ExecStart=/usr/bin/python3 -m rootkeel.agent
Restart=on-failure
RestartSec=3
# Preserve the Zellij server and live shell across an agent restart. The agent
# explicitly shuts down frpc on SIGTERM. Live shell lifetime is owner-controlled.
KillMode=process
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
LimitCORE=0
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/rootkeel
[Install]
WantedBy=multi-user.target
UNIT
if [ "$start_mode" = start ]; then
  systemctl daemon-reload
  systemctl enable --now rootkeel-terminal.service rootkeel-agent.service
fi
dpkg-query -W python3 python3-aiohttp ca-certificates curl > /var/lib/rootkeel/installed-packages.txt
target=$(python3 -c 'import json, sys; c = json.load(open(sys.argv[1]))
print("%s via %s:%s" % (c.get("environment", "unnamed environment"), c["relay_host"], c.get("relay_port", 443)))' \
  /var/lib/rootkeel/machine.json)
printf 'Agent installed for %s; enrolled with %s; session %s; loopback ports 8082/8083.\n' "$owner" "$target" "$session"
