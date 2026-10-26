# Install

This guide sets up one Rootkeel server with Docker Compose, signs `rt` in, and shares a first
Linux machine. It takes about ten minutes once DNS and a certificate are in place. To run the
server on Kubernetes instead, follow [helm.md](helm.md) for the server, then continue here from
[step 3](#3-sign-rt-in).

## What you need

| Where | What |
|---|---|
| Server | One Linux host with a public IP address, Docker with the Compose plugin (`scripts/compose.sh` fetches pinned Compose and Buildx plugins if they are missing), Python 3 and OpenSSL. Port 443 reachable from the Internet. |
| DNS | Three names pointing at the server, for example `term.example.com` (the API and browsers), `owner.term.example.com` (your terminal pages) and `term-relay.example.com` (the machines' tunnel). No wildcard record is needed. |
| Certificate | One TLS certificate covering all three names, for example from Let's Encrypt with three subject alternative names. |
| Machines to share | Debian 13 or Ubuntu 24.04 with systemd, x86-64 or ARM64, and outbound HTTPS. Nothing listens on the network. |
| Your computer | macOS or Linux; on Windows, use WSL. `rt` is a standalone download, or one Python 3 file from the checkout. [Zellij 0.45.1](https://github.com/zellij-org/zellij/releases/tag/v0.45.1) for `rt attach`. A browser is enough otherwise. |

## 1. Configure the server

Clone the release you want to run and copy the example settings to a private path:

```sh
git clone https://github.com/rootkeel/rootkeel.git
cd rootkeel
git checkout v0.1.0            # the release you run
sudo install -D -m 600 server/example.env /etc/rootkeel/rootkeel.env
sudoedit /etc/rootkeel/rootkeel.env
```

Set at least:

- `RT_PUBLIC_HOST` and `RT_RELAY_HOST`: your two hostnames (the terminal host is always
  `owner.` followed by `RT_PUBLIC_HOST`).
- `RT_STATE_DIR`: where the server keeps its private state, outside the checkout.
- `RT_TLS_CERT_FILE` and `RT_TLS_KEY_FILE`: your certificate chain and key.
- `RT_BROKER_IMAGE` and `RT_RELAY_IMAGE`: the release's images, or ones you build (see below).

Every setting is described in [configuration.md](configuration.md).

### Images

Each release is meant to publish its broker and relay images for `linux/amd64` and `linux/arm64`
to `ghcr.io/rootkeel/broker` and `ghcr.io/rootkeel/relay`, tagged with the version, with SBOM and
build provenance attestations. When the release you run lists them in its notes on the
[releases page](https://github.com/rootkeel/rootkeel/releases), use those: set `RT_BROKER_IMAGE`
and `RT_RELAY_IMAGE` to the digest references the notes print, for example

```sh
RT_BROKER_IMAGE=ghcr.io/rootkeel/broker@sha256:<digest from the release notes>
RT_RELAY_IMAGE=ghcr.io/rootkeel/relay@sha256:<digest from the release notes>
```

and set `RT_REQUIRE_IMAGE_DIGEST=true` if you like. Compose pulls them on the first start.

Otherwise, for a release without published images or a commit that is not a release, build the
broker and relay images yourself. On the server, from the checkout:

```sh
scripts/build-images.sh rootkeel-local 0.1.0
```

This produces `rootkeel-local/broker:0.1.0` and `rootkeel-local/relay:0.1.0`, the references the
example file already names. Without `--push` the images stay in the local Docker image store, which
is enough when you build on the server itself. To build elsewhere and push to your own registry,
run `scripts/build-images.sh --push registry.example.com/rootkeel 0.1.0-local-$(git rev-parse --short=12 HEAD)`
and put the printed references into `RT_BROKER_IMAGE` and `RT_RELAY_IMAGE`.

## 2. Prepare the state and start

```sh
sudo scripts/prepare.sh /etc/rootkeel/rootkeel.env
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env up -d
```

`prepare.sh` validates the settings, creates the state directory with the owner credential
(`<state>/owner`) and the database encryption key (`<state>/db-key`), and checks that the
certificate covers all three names and matches its key. It never replaces an existing credential,
key or database, so it is safe to run again.

For a local trial without a public certificate, `prepare.sh <env-file> --lab-ca` creates a
throwaway certificate authority and a certificate in `<state>/certs`; leave `RT_TLS_CERT_FILE` and
`RT_TLS_KEY_FILE` unset, and give clients the CA with `--ca-cert <state>/certs/ca.crt`. Never use
a lab CA for a public deployment.

Check the stack:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env ps
curl -sS https://term.example.com/ | grep -o '<title>.*</title>'
```

## 3. Sign rt in

Install `rt` on your computer, from the same release as the server. Either download the standalone
executable for your computer from the release (see [Release downloads](#release-downloads)) and
check it against the release's `SHA256SUMS`:

```sh
tar -xzf rt-0.1.0-macos-arm64.tar.gz
install -m 755 rt-0.1.0-macos-arm64/rt ~/.local/bin/rt
```

or install the Python 3 file from the checkout: `install -m 755 rt ~/.local/bin/rt`. Then:

```sh
rt login --url https://term.example.com
```

Paste the owner credential when asked; read it on the server with `sudo cat <state>/owner`.
`rt login` checks it against the server and keeps it in `~/.config/rt/<host>/credential`, readable
only by you, and remembers the URL. `rt logout` deletes that file again.

## 4. Enroll a machine

On your computer, ask for a one-use enrollment code (valid for five minutes) and use it:

```sh
rt enrollment --name laptop
rt enroll machine.json        # enter the code; writes machine.json (mode 0600)
```

The name is optional; it lets you choose the machine with `--machine laptop` later. Copy
`machine.json` privately to the Linux machine to share, then install the agent there from a
checkout of the same release, or from the release's agent bundle (`tar -xzf
rootkeel-agent-0.1.0-linux.tar.gz && cd rootkeel-agent-0.1.0`):

```sh
sudo agent/install.sh alice machine.json
```

`alice` is the ordinary account whose shell you share; it must not be root. The installer installs
Python's aiohttp from the distribution, checksum-verified Zellij 0.45.1 and frpc 0.71.0, a
dedicated `rt-agent` account that holds the machine credential, and two systemd units:
`rootkeel-terminal` (Zellij, as `alice`) and `rootkeel-agent` (the tunnel, as `rt-agent`). It
starts a Zellij session named `main`; set `RT_SESSION` to name it otherwise.

Repeat this step for every machine you want to share. `rt machines` lists them.

## 5. Open a terminal

From a terminal on your computer:

```sh
rt attach main                     # with several machines: rt attach main --machine laptop
```

`rt attach` issues a grant, connects, and reconnects by itself when the network drops. Detach with
`Ctrl+Alt+g`, then `d`; the session keeps running.

From a browser:

```sh
rt grant main
```

prints JSON with the terminal `url` and a session credential `token`. Open the URL and paste the
token once; the browser stays signed in to that session until you revoke the grant
(`rt grants`, then `rt revoke GRANT`).

To start another session, add `--create`: `rt attach build --create`.

## Release downloads

Each release lists these files, with a `SHA256SUMS` file to check them (`sha256sum -c SHA256SUMS`):

| Download | What it is | Runs on |
|---|---|---|
| `rt-<version>-macos-arm64.tar.gz` | `rt`, standalone | macOS on Apple silicon |
| `rt-<version>-macos-x86_64.tar.gz` | `rt`, standalone | macOS on Intel |
| `rt-<version>-linux-x86_64.tar.gz` | `rt`, standalone | Linux on x86-64 with glibc 2.35 or later (Ubuntu 22.04, Debian 12 and newer) |
| `rt-<version>-linux-arm64.tar.gz` | `rt`, standalone | Linux on ARM64, same C library |
| `rootkeel-agent-<version>-linux.tar.gz` | The agent installer with its files (`agent/install.sh`) | Debian 13 and Ubuntu 24.04, x86-64 and ARM64; it fetches the Zellij and frpc build for the machine's architecture |
| `ghcr.io/rootkeel/broker`, `ghcr.io/rootkeel/relay` (images, not files) | The server | Linux hosts with Docker, `linux/amd64` and `linux/arm64` |
| `rootkeel-<version>-images-sbom-provenance.tar.gz` | SPDX SBOMs and build provenance of both images, per platform | |

The standalone `rt` needs no Python; it carries its own Python runtime and uses the computer's
trusted certificates. Every `rt` command works on all of these; `rt attach` also needs Zellij 0.45.1
on the same computer. The macOS executables are not signed: if macOS refuses to open an `rt`
downloaded with a browser, remove the download mark with `xattr -d com.apple.quarantine rt`.

**Windows** is not supported natively. Run `rt` inside WSL, with the Linux download (and Zellij for
Linux inside WSL for `rt attach`).
