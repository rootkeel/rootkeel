# Install

This guide takes you from nothing to an open terminal: it sets up one Rootkeel server with Docker
Compose, installs `rt` on your computer and signs it in, and shares a first Linux machine. It
assumes you have never used Rootkeel before. Follow the steps in order; each one says where its
commands run and what you should see.

To run the server on Kubernetes instead, follow [helm.md](helm.md) for the server, then come back
here at [step 9](#9-install-rt-on-your-computer).

## How the pieces fit

You work with three kinds of computer. Every command in this guide is marked with where it runs.

| Label | What it is |
|---|---|
| **Server** | The Linux host that runs the Rootkeel server (three containers: the nginx front door, the broker and the relay). It needs a public IP address and is the only thing that accepts connections from the Internet. |
| **Your computer** | The laptop or desktop you work from. It runs `rt`, the command-line client, and a browser. |
| **Machine to share** | A Linux machine whose shell you want to reach from anywhere. It runs the Rootkeel agent, which dials out to the server. Nothing on it listens on the network. You can share many machines; the server itself can also be one. |

The guide uses these example values. Replace them with your own everywhere you see them:

| Example | Stands for |
|---|---|
| `term.example.com` | Your public hostname (`RT_PUBLIC_HOST`): the API, `rt` and browsers. |
| `owner.term.example.com` | Your terminal pages. Always `owner.` followed by the public hostname. |
| `term-relay.example.com` | The hostname the machines' tunnel dials (`RT_RELAY_HOST`). |
| `203.0.113.10` | The server's public IPv4 address. |
| `/opt/rootkeel-server` | Where the server's copy of Rootkeel lives on the server. |
| `/etc/rootkeel/rootkeel.env` | The server's settings file. |
| `/srv/rootkeel/prod` | The server's private state (`RT_STATE_DIR`). |
| `devbox` | The name you give the machine to share. |
| `alice` | The ordinary (non-root) account on the machine to share whose shell you share. |
| `0.1.0` | The release you install. Check the [releases page](https://github.com/rootkeel/rootkeel/releases) for the latest. |

## 1. Check the prerequisites

### On the server

| Need | Check with | Expect |
|---|---|---|
| Linux on x86-64 or ARM64, with root access through `sudo` | `uname -m` | `x86_64` or `aarch64` |
| A public IPv4 address | `ip -4 addr show scope global` on the server, or your provider's console | Your server's address, for example `203.0.113.10` |
| Docker Engine, running and enabled at boot | `sudo docker version --format '{{.Server.Version}}'` and `systemctl is-enabled docker` | A version number (these steps were checked with 28.2), then `enabled` |
| Git | `git --version` | `git version 2.…` |
| Python 3.9 or later | `python3 --version` | `Python 3.9` or later |
| OpenSSL 1.1.1 or later | `openssl version` | `OpenSSL 1.1.1…` or `OpenSSL 3.…` |
| curl | `curl --version` | `curl 7.…` or later |
| Nothing else listening on TCP port 443 | `sudo ss -ltnp 'sport = :443'` | Only the header line, no listener |

The Docker Compose and Buildx plugins are optional. When `docker compose version` or
`docker buildx version` fails, `scripts/compose.sh` downloads pinned, checksum-verified copies
(Compose 5.5.1 and Buildx 0.37.1) into `<state>/docker-config` the first time it runs and uses them
from there; nothing is installed system-wide.

On Debian or Ubuntu, install the missing tools with
`sudo apt-get install -y git python3 openssl curl`, and Docker by following
[Docker's instructions](https://docs.docker.com/engine/install/).

### On your computer

| Need | Check with | Expect |
|---|---|---|
| macOS (Apple silicon or Intel) or Linux (x86-64 or ARM64). On Windows, use WSL and follow the Linux steps inside it. | `uname -sm` | `Darwin arm64`, `Darwin x86_64`, `Linux x86_64` or `Linux aarch64` |
| On Linux: glibc 2.35 or later, for the standalone `rt` (Ubuntu 22.04, Debian 12 and newer) | `ldd --version \| head -1` | `… 2.35` or higher |
| curl and a SHA-256 tool | `curl --version`; on Linux `sha256sum --version`, on macOS `shasum --version` | Version output |
| Optional, for `rt attach` in a terminal: Zellij 0.45.1 exactly | `zellij --version` | `zellij 0.45.1` |

Without Zellij you can still do everything else, and open terminals in a browser.

### On each machine to share

| Need | Check with | Expect |
|---|---|---|
| Debian 13 or Ubuntu 24.04, with systemd | `grep PRETTY_NAME /etc/os-release` | `Debian GNU/Linux 13 (trixie)` or `Ubuntu 24.04…` |
| x86-64 or ARM64 | `uname -m` | `x86_64` or `aarch64` |
| Root access through `sudo`, and an ordinary account to share (not root) | `id -u alice` | A number other than `0` |
| Outbound HTTPS to the server, to github.com (the installer downloads Zellij and frpc from there) and to your distribution's package mirror | `curl -sSI https://github.com \| head -1` | `HTTP/2 200` |

## 2. Create the DNS records

Rootkeel needs three hostnames, all pointing at the server. Derive them from your public hostname:

| Record name | Type | Value | Used for |
|---|---|---|---|
| `term.example.com` | `A` | `203.0.113.10` | The API, `rt` and the browser's landing page (`RT_PUBLIC_HOST`). |
| `owner.term.example.com` | `A` | `203.0.113.10` | Your terminal pages. The name is fixed: `owner.` followed by `RT_PUBLIC_HOST`. |
| `term-relay.example.com` | `A` | `203.0.113.10` | The machines' tunnel (`RT_RELAY_HOST`). Any name works if it differs from the other two. |

You may use `CNAME` records pointing at `term.example.com` for the second and third names instead.
No wildcard record is needed. Do not add `AAAA` (IPv6) records for these names: the example
settings make the server listen on IPv4 only (`RT_HTTPS_BIND=0.0.0.0:443`), so clients that prefer
IPv6 would fail to connect.

Create the records at your DNS provider, wait a few minutes, then check them **from your
computer**:

```sh
for name in term.example.com owner.term.example.com term-relay.example.com; do
  printf '%s -> %s\n' "$name" "$(dig +short "$name" A | tail -1)"
done
```

Each line must end in your server's address, for example
`owner.term.example.com -> 203.0.113.10`. An empty value means the record does not exist yet or
has not propagated. (Without `dig`, use `nslookup term.example.com` or, on Linux,
`getent hosts term.example.com`.)

## 3. Open the firewall

Only one port has to be reachable from the Internet:

| Where | Direction | Port | Why |
|---|---|---|---|
| Server | Inbound | TCP 443 | Browsers, `rt` and the machines' tunnel all arrive here (`RT_HTTPS_BIND`, `RT_PUBLIC_PORT`). |
| Server | Inbound, optional | TCP 80 | Only while `certbot --standalone` obtains or renews a Let's Encrypt certificate (step 5). Rootkeel itself never uses port 80. |
| Server | Outbound | TCP 443 | Pulling images and, on the first start, the Compose plugin. |
| Your computer | Outbound | TCP 443 | To the server. |
| Machine to share | Outbound | TCP 443 | To the server's relay hostname, to github.com during installation, and to package mirrors. Nothing inbound. |

Open TCP 443 (and 80 if you use `certbot --standalone`) in your cloud provider's security group or
network firewall. On a host firewall such as `ufw`: `sudo ufw allow 443/tcp`. Docker publishes the
server's port with its own firewall rules, which take effect even when `ufw` would deny the port,
so the provider's firewall is the one that decides.

## 4. Get Rootkeel on the server

**On the server**, clone the repository to a permanent location and check out the release you
install. The Compose stack reads its nginx and relay configuration from this directory, so keep it
in place for as long as the server runs. Do not use `/opt/rootkeel`: the agent installer uses that
path if you also share the server itself as a machine.

```sh
sudo git clone https://github.com/rootkeel/rootkeel.git /opt/rootkeel-server
cd /opt/rootkeel-server
sudo git checkout v0.1.0
sudo git describe --tags
```

The last command prints `v0.1.0`. (The checkout belongs to root, so run `git` in it with `sudo`.) All remaining server commands run from `/opt/rootkeel-server`.

## 5. Get a TLS certificate

The server needs one certificate that covers all three hostnames, as two PEM files:

- the certificate **full chain**: your certificate first, then the intermediate certificates;
- its **private key**, unencrypted (no passphrase), readable by root only (mode `600`).

The files may live anywhere on the server; step 6 names them in the settings file. The front door
reads them when it starts. A wildcard certificate for `*.term.example.com` covers
`owner.term.example.com` but not `term-relay.example.com`, so that name must still be in it.

### Option A: Let's Encrypt with certbot

**On the server**, with TCP port 80 open (step 3) and the DNS records from step 2 in place:

```sh
sudo apt-get install -y certbot
sudo certbot certonly --standalone \
  -d term.example.com -d owner.term.example.com -d term-relay.example.com
```

certbot writes the files to:

| File | Path |
|---|---|
| Full chain | `/etc/letsencrypt/live/term.example.com/fullchain.pem` |
| Private key | `/etc/letsencrypt/live/term.example.com/privkey.pem` |

Use those two paths in step 6. certbot renews the certificate by itself (it also needs port 80 at
renewal time, or switch to a DNS-01 plugin). Make it restart the front door after each renewal,
so the new certificate is served:

```sh
sudo tee /etc/letsencrypt/renewal-hooks/deploy/rootkeel.sh >/dev/null <<'EOF'
#!/bin/sh
cd /opt/rootkeel-server && scripts/compose.sh /etc/rootkeel/rootkeel.env restart proxy
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/rootkeel.sh
```

### Option B: a certificate you already have

**On the server**, copy the two files into place with the right permissions:

```sh
sudo install -D -m 644 fullchain.pem /etc/rootkeel/tls/fullchain.pem
sudo install -D -m 600 privkey.pem /etc/rootkeel/tls/privkey.pem
```

and use `/etc/rootkeel/tls/fullchain.pem` and `/etc/rootkeel/tls/privkey.pem` in step 6. When you
renew it, replace the files and restart the front door (see [Certificates](#certificates)).

### Check the certificate

Whichever option you chose, check that it names all three hostnames (use your certificate's path):

```sh
sudo openssl x509 -in /etc/letsencrypt/live/term.example.com/fullchain.pem -noout -ext subjectAltName -enddate
```

The output lists `DNS:term.example.com`, `DNS:owner.term.example.com` and
`DNS:term-relay.example.com` (in any order), and the expiry date. Step 7 checks this again, and
that the key matches.

### Only trying it out?

For a private trial without public DNS or a public certificate, `scripts/prepare.sh` can create a
throwaway certificate authority instead: leave `RT_TLS_CERT_FILE` and `RT_TLS_KEY_FILE` unset in
step 6, and in step 7 run `sudo scripts/prepare.sh /etc/rootkeel/rootkeel.env --lab-ca`. It writes
`<state>/certs/ca.crt`, `server.crt` and `server.key`, valid for 30 days. Every client must then
trust that CA: copy `ca.crt` to your computer and add `--ca-cert ca.crt` to every `rt` command, or
set `RT_CA_CERT` to its path. Never use a lab CA for a server reachable from the Internet.

## 6. Write the settings file

**On the server**, copy the example settings to a private file and open it in an editor:

```sh
cd /opt/rootkeel-server
sudo install -D -m 600 server/example.env /etc/rootkeel/rootkeel.env
sudoedit /etc/rootkeel/rootkeel.env
```

Set every line as follows. Values are literal: no quotes needed, no `$` expansion.

| Setting | Put | Notes |
|---|---|---|
| `RT_ENVIRONMENT` | `prod` | A short name: lowercase letters, digits and hyphens, starting with a letter, at most 30 characters. It names the Compose project `rootkeel-prod`. |
| `RT_PUBLIC_HOST` | `term.example.com` | A bare hostname: no `https://`, port or path. |
| `RT_RELAY_HOST` | `term-relay.example.com` | Must differ from `RT_PUBLIC_HOST` and from `owner.<RT_PUBLIC_HOST>`. |
| `RT_PUBLIC_PORT` | `443` | The port clients connect to. Keep 443 unless you have a reason not to. |
| `RT_HTTPS_BIND` | `0.0.0.0:443` | The server address and port Docker listens on. Its port matches `RT_PUBLIC_PORT` unless something in front of the server maps one to the other. |
| `RT_STATE_DIR` | `/srv/rootkeel/prod` | An absolute path outside `/opt/rootkeel-server`. Step 7 creates it. |
| `RT_TLS_CERT_FILE` | The full chain from step 5 | For example `/etc/letsencrypt/live/term.example.com/fullchain.pem`. Comment the line out (`#`) only for a lab CA. |
| `RT_TLS_KEY_FILE` | The private key from step 5 | For example `/etc/letsencrypt/live/term.example.com/privkey.pem`. |
| `RT_HSTS_MAX_AGE` | `31536000` | How long browsers insist on HTTPS for these names. Use `300` while you may still change hostnames. |
| `RT_REQUIRE_IMAGE_DIGEST` | `true` | Accept only images pinned by digest. |
| `RT_BROKER_IMAGE` | The broker image of the release | For 0.1.0: `ghcr.io/rootkeel/broker@sha256:e901ad1ef899ff14f4d2f2d960232425e2af834976c6013a45829d0fe9bd4efb` |
| `RT_RELAY_IMAGE` | The relay image of the release | For 0.1.0: `ghcr.io/rootkeel/relay@sha256:7043f2d96ab4bdcf92b7a76168fcddb99741eaa679402bb3e0e8c86789f268ee` |
| `RT_PROXY_IMAGE` | Leave as it is | The nginx image the release pins. |
| `RT_GRANT_IDLE_TIMEOUT`, `RT_GRANT_MAX_LIFETIME` | Leave commented out | Optional time limits on session grants; see [configuration.md](configuration.md). |

The image digests are printed under "Server images" in each release's notes on the
[releases page](https://github.com/rootkeel/rootkeel/releases); copy them from the release you
checked out. The images are public: no registry login is needed.

The finished file for this guide looks like this:

```sh
RT_ENVIRONMENT=prod
RT_PUBLIC_HOST=term.example.com
RT_RELAY_HOST=term-relay.example.com
RT_PUBLIC_PORT=443
RT_HTTPS_BIND=0.0.0.0:443
RT_STATE_DIR=/srv/rootkeel/prod
RT_TLS_CERT_FILE=/etc/letsencrypt/live/term.example.com/fullchain.pem
RT_TLS_KEY_FILE=/etc/letsencrypt/live/term.example.com/privkey.pem
RT_HSTS_MAX_AGE=31536000
RT_REQUIRE_IMAGE_DIGEST=true
RT_BROKER_IMAGE=ghcr.io/rootkeel/broker@sha256:e901ad1ef899ff14f4d2f2d960232425e2af834976c6013a45829d0fe9bd4efb
RT_RELAY_IMAGE=ghcr.io/rootkeel/relay@sha256:7043f2d96ab4bdcf92b7a76168fcddb99741eaa679402bb3e0e8c86789f268ee
RT_PROXY_IMAGE=nginx:1.30.1-alpine@sha256:c819f83c54b0361f5557601bf5eb4943d09360e7a7fdf426afc466570f45874d
```

There are no passwords or keys to invent: the server's two secrets, the owner credential and the
database encryption key, are generated in step 7 (each is 256 random bits from
`openssl rand -hex 32`) and never go in this file.

Check the file:

```sh
sudo python3 -m rootkeel.config check-env /etc/rootkeel/rootkeel.env
```

It prints `OK prod: https://term.example.com relay term-relay.example.com`. Otherwise it lists
every problem, each naming the setting to fix; fix them and run it again.

Every setting is described in [configuration.md](configuration.md).

### Building the images yourself

For a commit that is not a release, or if you prefer not to use the published images, build the
broker and relay **on the server**, from `/opt/rootkeel-server`:

```sh
sudo scripts/build-images.sh rootkeel-local 0.1.0
```

It prints one line per image, for example `rootkeel-local/broker:0.1.0 sha256:…`. These are the
references `server/example.env` already names: keep `RT_BROKER_IMAGE=rootkeel-local/broker:0.1.0`,
`RT_RELAY_IMAGE=rootkeel-local/relay:0.1.0` and `RT_REQUIRE_IMAGE_DIGEST=false`. The images stay in
the server's local Docker image store. A version tag can be built only once; use a new one, such as
`0.2.0`, for each later build. To build elsewhere and push to
your own registry, run `scripts/build-images.sh --push registry.example.com/rootkeel <version>` and
use the digest references it prints.

## 7. Prepare the state

**On the server**:

```sh
cd /opt/rootkeel-server
sudo scripts/prepare.sh /etc/rootkeel/rootkeel.env
```

Expected output:

```
OK prod: https://term.example.com relay term-relay.example.com
Environment prod state ready in /srv/rootkeel/prod; no credentials printed.
The owner credential is in /srv/rootkeel/prod/owner: give it to rt login on your computer (sudo cat /srv/rootkeel/prod/owner).
```

`prepare.sh` checks the settings, then creates:

| Path | What | Owner and mode |
|---|---|---|
| `/srv/rootkeel/prod/` | The state directory | `root`, `700` |
| `/srv/rootkeel/prod/owner` | The owner credential: 64 hex characters | uid 65534 (`nobody`), `600` |
| `/srv/rootkeel/prod/db-key` | The database encryption key | uid 65534, `600` |
| `/srv/rootkeel/prod/db/` | The broker's database (filled on first start) | uid 65534, `700` |
| `/srv/rootkeel/prod/certs/` | The lab CA, only with `--lab-ca` | `root`, `700` |

It then checks that the certificate covers `term.example.com`, `owner.term.example.com` and
`term-relay.example.com`, and that the key belongs to the certificate. It never replaces an
existing credential, key or database, so running it again is always safe.

## 8. Start the server

**On the server**:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env up -d
```

The first run may download the Compose plugin (it prints `…/docker-compose: OK` after checking its
checksum), then pulls the three images and ends with these lines, in any order:

```
 Container rootkeel-prod-broker-1 Started
 Container rootkeel-prod-proxy-1 Started
 Container rootkeel-prod-relay-1 Started
```

The containers restart by themselves after a crash or a reboot, as long as Docker is enabled at
boot.

### Check that it is healthy

**On the server**, list the containers:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env ps
```

All three, `rootkeel-prod-broker-1`, `rootkeel-prod-proxy-1` and `rootkeel-prod-relay-1`, show
`Up` in the `STATUS` column, and the proxy's `PORTS` column includes `0.0.0.0:443->443/tcp`. A status
of `Restarting` means a container keeps failing; see [Troubleshooting](#troubleshooting).

Look at the logs:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env logs --tail 20 proxy broker relay
```

The front door's start-up ends with
`proxy-1  | /docker-entrypoint.sh: Configuration complete; ready for start up`, and afterwards it
logs one line per request: a timestamp and the HTTP status, for example
`proxy-1  | 2026-01-01T12:00:00+00:00 200`. The broker and the relay log nothing while all is well.

**From your computer**, check each hostname:

```sh
curl -sS https://term.example.com/ | grep -o '<title>.*</title>'
curl -sS -o /dev/null -w '%{http_code}\n' https://owner.term.example.com/
curl -sS -o /dev/null -w '%{http_code}\n' https://term-relay.example.com/
```

Expected, in order: `<title>Rootkeel sign-in</title>`, `200` and `404` (the relay hostname serves
only the machines' tunnel). Opening `https://term.example.com/` in a browser shows a page titled
"Open a terminal session". Any certificate warning, `Could not resolve host` or time-out is covered
in [Troubleshooting](#troubleshooting).

## 9. Install rt on your computer

`rt` is the command-line client. Install the one from the same release as the server.

**On your computer**, pick your platform from the output of `uname -sm`:

| `uname -sm` prints | `PLATFORM` |
|---|---|
| `Darwin arm64` | `macos-arm64` |
| `Darwin x86_64` | `macos-x86_64` |
| `Linux x86_64` | `linux-x86_64` |
| `Linux aarch64` | `linux-arm64` |

Then download it, check it and install it into `~/.local/bin`:

```sh
VERSION=0.1.0
PLATFORM=linux-x86_64          # from the table above
mkdir -p ~/rootkeel-download && cd ~/rootkeel-download
curl -fLO "https://github.com/rootkeel/rootkeel/releases/download/v$VERSION/rt-$VERSION-$PLATFORM.tar.gz"
curl -fLO "https://github.com/rootkeel/rootkeel/releases/download/v$VERSION/SHA256SUMS"
grep " rt-$VERSION-$PLATFORM.tar.gz\$" SHA256SUMS | sha256sum -c -        # on macOS: shasum -a 256 -c -
```

The check prints `rt-0.1.0-linux-x86_64.tar.gz: OK` (with your platform). Stop if it prints
`FAILED`: the download is damaged or not the release's file. Then:

```sh
tar -xzf "rt-$VERSION-$PLATFORM.tar.gz"
mkdir -p ~/.local/bin
install -m 755 "rt-$VERSION-$PLATFORM/rt" ~/.local/bin/rt
command -v rt
rt --help | head -1
```

`command -v rt` prints the path, for example `/home/you/.local/bin/rt`. If it prints nothing,
`~/.local/bin` is not on your `PATH`: add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`
(macOS) or `~/.bashrc` (Linux), open a new terminal and try again. `rt --help | head -1` prints
`usage: rt [-h] [--url URL] [--credential-file CREDENTIAL_FILE]`. You can now delete
`~/rootkeel-download`.

The standalone `rt` needs no Python. On macOS, if you downloaded the archive with a browser instead
of `curl` and macOS refuses to open `rt`, remove the download mark with
`xattr -d com.apple.quarantine ~/.local/bin/rt`.

Alternatively, with Python 3.9 or later on your computer, install the single `rt` file from a
checkout of the same release: `install -m 755 rt ~/.local/bin/rt`.

For `rt attach` (step 12), also install [Zellij 0.45.1](https://github.com/zellij-org/zellij/releases/tag/v0.45.1)
on your computer; `zellij --version` must print `zellij 0.45.1`.

## 10. Sign rt in with the owner credential

The owner credential is the one secret that controls your server: whoever holds it can enroll
machines and open terminals. `prepare.sh` wrote it to `<state>/owner` in step 7.

**On the server**, print it:

```sh
sudo cat /srv/rootkeel/prod/owner
```

It is a single line of 64 hexadecimal characters. Copy it.

**On your computer**, sign in:

```sh
rt login --url https://term.example.com
```

At the prompt `Owner credential (the server's <state>/owner file):`, paste the credential and
press Enter; nothing is shown while you paste. Expected output:

```
Signed in to term.example.com. The owner credential is in /home/you/.config/rt/term.example.com/credential, readable only by you.
No machine is enrolled yet. Next: rt enrollment, then rt enroll on the machine to share.
```

`rt login` checks the credential against the server before saving anything. It keeps the
credential in `~/.config/rt/term.example.com/credential` (mode `600`) and the URL in
`~/.config/rt/url`, so later commands need neither. `rt logout` deletes the saved copy. Clear the
credential from your clipboard afterwards.

## 11. Share a machine

Sharing a machine takes three short steps: ask the server for a one-use enrollment code, exchange
it for a machine credential file, and install the agent with that file on the machine.

### 11.1 Get an enrollment code

**On your computer**:

```sh
rt enrollment --name devbox
```

Expected output:

```
Enrollment code (single use, valid 5 minutes): <a long random code>
Next: rt enroll machine.json --url https://term.example.com, enter this code, then copy machine.json to the machine to share and run sudo agent/install.sh <user> machine.json there.
```

The name (`devbox`) is optional; it lets you pick this machine later with `--machine devbox`.
Names are letters, digits, `-` and `_`, starting with a letter or digit.

### 11.2 Exchange the code for a machine credential

Within five minutes, **on your computer**:

```sh
rt enroll machine.json
```

At the prompt `One-use enrollment code:`, paste the code. Expected output:

```
Machine credential for devbox in environment prod saved; its agent dials term-relay.example.com:443 only. Transfer it privately to the Linux machine and run agent/install.sh there.
```

`machine.json` (mode `600`) is the machine's own credential. Treat it like a password.

### 11.3 Copy the credential to the machine

**On your computer**, copy it to the machine's shared account, then delete your copy:

```sh
scp machine.json alice@devbox:
rm machine.json
```

(Use any private channel you like instead of `scp`; just never paste the file somewhere public.)

### 11.4 Install the agent

**On the machine to share**, logged in as `alice`, download the agent bundle of the same release
and check it:

```sh
VERSION=0.1.0
mkdir -p ~/rootkeel-download && cd ~/rootkeel-download
curl -fLO "https://github.com/rootkeel/rootkeel/releases/download/v$VERSION/rootkeel-agent-$VERSION-linux.tar.gz"
curl -fLO "https://github.com/rootkeel/rootkeel/releases/download/v$VERSION/SHA256SUMS"
grep " rootkeel-agent-$VERSION-linux.tar.gz\$" SHA256SUMS | sha256sum -c -
```

The check prints `rootkeel-agent-0.1.0-linux.tar.gz: OK`. Then unpack it and run the installer,
naming the account to share and the credential file:

```sh
tar -xzf "rootkeel-agent-$VERSION-linux.tar.gz"
cd "rootkeel-agent-$VERSION"
sudo agent/install.sh alice ~/machine.json
```

The installer:

- installs `python3`, `python3-aiohttp`, `ca-certificates` and `curl` from the distribution;
- downloads Zellij 0.45.1 and frpc 0.71.0 from GitHub, checks their SHA-256 checksums and installs
  them in `/usr/local/bin`, with `rt` next to them;
- copies the agent to `/opt/rootkeel`;
- creates a system account `rt-agent` and gives it the credential, as
  `/var/lib/rootkeel/machine.json` (mode `600`); `alice` cannot read it;
- writes and starts two systemd units: `rootkeel-terminal` (Zellij, running as `alice`) and
  `rootkeel-agent` (the tunnel, running as `rt-agent`);
- starts a Zellij session named `main`. To name it otherwise, run the installer as
  `sudo RT_SESSION=work agent/install.sh alice ~/machine.json`.

Its last line is:

```
Agent installed for alice; enrolled with prod via term-relay.example.com:443; session main; loopback ports 8082/8083.
```

Check that both services run:

```sh
systemctl is-active rootkeel-terminal rootkeel-agent
```

prints `active` twice. The installer has taken the original `~/machine.json` over (it now belongs
to root), so remove it, and the download, with `rm -f ~/machine.json && rm -rf ~/rootkeel-download`.

**On your computer**, `rt machines` now lists the machine:

```
MACHINE                     NAME    STATE    ENROLLED
m_0123456789abcdef01234567  devbox  enabled  2026-01-01 12:00
```

Repeat step 11 for every machine you want to share.

## 12. Open a terminal

### In a browser

**On your computer**:

```sh
rt grant main
```

prints one line of JSON with the fields `id` (the grant, `g_…`), `machine`, `url`, `token`,
`expires` and `permanent`. The `url` looks like `https://owner.term.example.com/g/g_…/main`.
Open the `url` in any browser, on any device, and paste the `token` into the
"Sign in to the terminal" form. The browser stays signed in to that session until you revoke the
grant: `rt grants` lists open grants, `rt revoke g_…` ends one. With several machines, add
`--machine devbox`.

### In a terminal

With Zellij 0.45.1 installed on your computer:

```sh
rt attach main
```

`rt attach` issues a grant, connects, and reconnects by itself when the network drops. Detach with
`Ctrl+Alt+g`, then `d`; the session keeps running on the machine. To start another session, add
`--create`: `rt attach build --create`.

You are done. The rest of this page is for later.

## Day-to-day operations

All server commands run **on the server** from `/opt/rootkeel-server`. `compose.sh` passes
everything after the settings file to `docker compose`. More, such as revoking access and rotating
the owner credential, is in [operations.md](operations.md).

### Stop and start

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env stop     # stop the containers; start brings them back
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env start
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env down     # remove the containers; the state stays
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env up -d    # create and start them again
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env restart  # restart all three
```

Stopping the server does not stop any session on the machines; their agents reconnect when the
server is back, and so does `rt attach`.

### Back up

The state directory holds everything that cannot be recreated. Back up the database and its key
**together**, but store them **apart**: the database is useless without the key, and the key is
what protects the machine credentials inside it.

```sh
sudo sh -c 'umask 077; mkdir -p /root/rootkeel-backup && python3 -c "import sqlite3, sys; sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))" /srv/rootkeel/prod/db/broker.sqlite /root/rootkeel-backup/broker-$(date +%F).sqlite'
```

This takes a consistent copy while the server runs. Also keep, somewhere else, copies of
`/srv/rootkeel/prod/db-key`, `/srv/rootkeel/prod/owner` and `/etc/rootkeel/rootkeel.env`. Details,
including what to do after losing the database, are in [operations.md](operations.md#backups).

To restore, stop the broker, put the copy back owned by uid 65534, and start it again:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env stop broker
sudo rm -f /srv/rootkeel/prod/db/broker.sqlite-wal /srv/rootkeel/prod/db/broker.sqlite-shm
sudo install -o 65534 -g 65534 -m 600 /root/rootkeel-backup/broker-2026-01-01.sqlite /srv/rootkeel/prod/db/broker.sqlite
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env start broker
```

`db-key` in the state directory must be the key that was in place when the backup was taken.

### Certificates

The front door reads the certificate only when it starts. After renewing it (certbot's deploy hook
from step 5 does this for you), restart it:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env restart proxy
```

### Upgrade

1. Read the new release's notes on the [releases page](https://github.com/rootkeel/rootkeel/releases).
2. Back up, as above.
3. **On the server**, check out the new release (here `v0.2.0`):

   ```sh
   cd /opt/rootkeel-server
   sudo git fetch --tags
   sudo git checkout v0.2.0
   ```

4. Compare `server/example.env` with your `/etc/rootkeel/rootkeel.env` (`sudo git diff v0.1.0
   v0.2.0 -- server/example.env` shows what changed). Set `RT_BROKER_IMAGE` and `RT_RELAY_IMAGE` to the new
   release's digests, and `RT_PROXY_IMAGE` to the value in the new `server/example.env`.
5. Apply it:

   ```sh
   sudo scripts/prepare.sh /etc/rootkeel/rootkeel.env
   sudo scripts/compose.sh /etc/rootkeel/rootkeel.env up -d
   ```

   Compose replaces the containers whose image changed. Check the server as in step 8.
6. **On your computer**, install the new `rt` as in step 9.
7. **On each machine**, download and unpack the new agent bundle as in step 11.4 and run its
   installer with the credential already installed:
   `sudo agent/install.sh alice /var/lib/rootkeel/machine.json`. Restarting the agent signs open
   browser tabs back in to Zellij by themselves; the sessions keep running.

### Uninstall

**A machine.** On your computer, `rt machine-revoke devbox` disables its credential and ends its
grants. Then on the machine:

```sh
sudo systemctl disable --now rootkeel-agent rootkeel-terminal
sudo rm -rf /var/lib/rootkeel /var/lib/rootkeel-helper /opt/rootkeel /etc/systemd/system/rootkeel-*.service /etc/sysctl.d/60-rootkeel.conf
sudo systemctl daemon-reload
sudo rm -f /usr/local/bin/zellij /usr/local/bin/frpc /usr/local/bin/rt
sudo userdel rt-agent
```

The Zellij session itself keeps running until you end it (as `alice`: `zellij kill-session main`)
or reboot.

**rt on your computer.** `rt logout`, then `rm ~/.local/bin/rt` and `rm -rf ~/.config/rt`.

**The server.** This deletes the owner credential and the database for good; back up first if
you may want them again.

```sh
cd /opt/rootkeel-server
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env down --remove-orphans
sudo docker image rm ghcr.io/rootkeel/broker@sha256:e901ad1ef899ff14f4d2f2d960232425e2af834976c6013a45829d0fe9bd4efb \
  ghcr.io/rootkeel/relay@sha256:7043f2d96ab4bdcf92b7a76168fcddb99741eaa679402bb3e0e8c86789f268ee
sudo rm -rf /srv/rootkeel/prod /etc/rootkeel /opt/rootkeel-server
sudo rm -f /etc/letsencrypt/renewal-hooks/deploy/rootkeel.sh
sudo certbot delete --cert-name term.example.com     # if you used certbot
```

Remove the three DNS records, and close port 443 if nothing else uses it.

## Troubleshooting

Start with the server's view: `sudo scripts/compose.sh /etc/rootkeel/rootkeel.env ps` and
`… logs --tail 100 proxy broker relay`. On a machine, `journalctl -u rootkeel-agent -u
rootkeel-terminal` shows the agent and the terminal helper.

### A hostname does not resolve

**Symptoms:** `curl: (6) Could not resolve host: term.example.com`; `rt` prints
`rt failed ([Errno -2] Name or service not known)` on Linux, or
`rt failed ([Errno 8] nodename nor servname provided, or not known)` on macOS.

**Fix:** check the three records as in step 2. A record created minutes ago may not have reached
your resolver yet; `dig +short term.example.com @<your DNS provider's nameserver>` asks the
authoritative server directly. Check
for typos, and that `owner.term.example.com` exists: it is easy to forget.

### The certificate does not match

**Symptoms during `prepare.sh`:**

- `TLS certificate /etc/…/fullchain.pem does not cover owner.term.example.com`: the certificate
  lacks one of the three names. Get one that includes all three (step 5).
- `TLS key /etc/…/privkey.pem does not match certificate /etc/…/fullchain.pem`: the two settings
  name files from different certificates.
- `TLS certificate/key missing: … / …`: a path in `RT_TLS_CERT_FILE` or `RT_TLS_KEY_FILE` is wrong,
  or the file is empty.

**Symptoms on clients:** a browser warning, `curl: (60) SSL certificate problem`, or `rt failed
([SSL: CERTIFICATE_VERIFY_FAILED] …)`. Check what the server actually presents for a name:

```sh
openssl s_client -connect term.example.com:443 -servername owner.term.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName -enddate
```

If it shows an old certificate, restart the front door
(`sudo scripts/compose.sh /etc/rootkeel/rootkeel.env restart proxy`). If it says `self-signed
certificate in certificate chain`, the server uses a lab CA or your full chain is incomplete: with
a lab CA, pass `--ca-cert ca.crt` to `rt`; otherwise make `RT_TLS_CERT_FILE` the full chain
(`fullchain.pem`, not `cert.pem`).

**`421 Misdirected Request`:** the request used a name the server does not serve, such as the
server's IP address or a hostname not set in `RT_PUBLIC_HOST`. Use the configured hostnames.

### The port is blocked or taken

**Symptoms on clients:** `curl` or `rt` waits, then fails with `timed out` (a firewall drops the
traffic), or fails at once with `Connection refused` (nothing listens: the server is not running,
or a firewall rejects).

**Fix:** on the server, `sudo ss -ltnp 'sport = :443'` must show `docker-proxy` listening (or
nothing, on some Docker set-ups that forward with firewall rules only), and `ps` must show the proxy
`Up` with `0.0.0.0:443->443/tcp` in its ports. Then check the provider's firewall for inbound TCP 443 (step 3).
From the server itself, `curl -sS --resolve term.example.com:443:127.0.0.1 https://term.example.com/`
tells you whether the server works when the network is out of the way.

**Symptom on `up -d`:** `Bind for 0.0.0.0:443 failed: port is already allocated` or `address
already in use`: another program or container holds port 443. `sudo ss -ltnp 'sport = :443'` names
it; stop it, or bind Rootkeel elsewhere with `RT_HTTPS_BIND` and `RT_PUBLIC_PORT`.

### The credential is rejected

- `rt login refused (401): Owner credential required. Nothing was saved; check the credential and
  --url.`: the pasted value is not the server's current owner credential. Copy it again with
  `sudo cat /srv/rootkeel/prod/owner`, without extra characters, and check that `--url` is this
  server.
- `rt login refused: an owner credential is the single line of at least 32 characters …`: the
  paste was cut short or empty.
- Any later `rt` command fails with `refused (401): Owner credential required`: the credential was
  rotated on the server ([operations.md](operations.md#rotating-the-owner-credential)); run
  `rt login` again.
- `rt refused (401): Enrollment code invalid or consumed`: the enrollment code was mistyped, already
  used, or older than five minutes. Get a new one with `rt enrollment`.
- `rt enrollment refused (409): An enrolled machine is already named devbox`: pick another name, or
  revoke the old machine first.
- The browser says `Grant invalid or ended. Request a new session.`: the session credential is
  wrong, or the grant was revoked. Run `rt grant main` again.

### The settings file is refused

`prepare.sh`, `compose.sh` and `check-env` print `Invalid environment file …` followed by one line
per problem, for example
`RT_PUBLIC_HOST is not a valid DNS name: 'https://term.example.com' (…no scheme, port, path or
wildcard)`. Fix each named setting. `Run preparation as root on the server` means `prepare.sh` was
run without `sudo`.

### The machine does not come online

`rt grant main` fails with `rt grant refused (503): Machine unavailable`, or `rt attach` keeps
retrying. **On the machine**:

```sh
systemctl is-active rootkeel-terminal rootkeel-agent
journalctl -u rootkeel-agent -n 50
getent hosts term-relay.example.com
curl -sS -o /dev/null -w '%{http_code}\n' https://term-relay.example.com/
```

Both units must be `active`, the relay name must resolve to the server, and the `curl` must print
`404` (the server answering). If it does, but the agent still cannot connect, check that the machine
was not revoked (`rt machines` on your computer shows `enabled`); re-enroll it otherwise
(step 11, then run the installer again with the new `machine.json`).

## Release downloads

Each release lists these files, with a `SHA256SUMS` file to check them:

| Download | What it is | Runs on |
|---|---|---|
| `rt-<version>-macos-arm64.tar.gz` | `rt`, standalone | macOS on Apple silicon |
| `rt-<version>-macos-x86_64.tar.gz` | `rt`, standalone | macOS on Intel |
| `rt-<version>-linux-x86_64.tar.gz` | `rt`, standalone | Linux on x86-64 with glibc 2.35 or later (Ubuntu 22.04, Debian 12 and newer) |
| `rt-<version>-linux-arm64.tar.gz` | `rt`, standalone | Linux on ARM64, same C library |
| `rootkeel-agent-<version>-linux.tar.gz` | The agent installer with its files (`agent/install.sh`) | Debian 13 and Ubuntu 24.04, x86-64 and ARM64; it fetches the Zellij and frpc build for the machine's architecture |
| `ghcr.io/rootkeel/broker`, `ghcr.io/rootkeel/relay` (images, not files) | The server | Linux hosts with Docker, `linux/amd64` and `linux/arm64` |
| `rootkeel-<version>-images-sbom-provenance.tar.gz` | SPDX SBOMs and build provenance of both images, per platform | |

The download URLs are
`https://github.com/rootkeel/rootkeel/releases/download/v<version>/<file>`. The standalone `rt`
carries its own Python runtime and uses the computer's trusted certificates. Every `rt` command
works on all of these; `rt attach` also needs Zellij 0.45.1 on the same computer. The macOS
executables are not signed.

**Windows** is not supported natively. Run `rt` inside WSL, with the Linux download (and Zellij for
Linux inside WSL for `rt attach`).
