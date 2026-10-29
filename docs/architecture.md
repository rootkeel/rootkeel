# Architecture

Rootkeel connects browsers and `rt` on your computers to Zellij sessions on your Linux machines,
through a server you run. The machines dial out to the server; nothing on them is reachable from
the network.

## Components

| Component | Runs on | Code | Role |
|---|---|---|---|
| Front door | server | nginx, `server/config/` | Terminates TLS. Routes `RT_PUBLIC_HOST` and `owner.RT_PUBLIC_HOST` to the broker and only the tunnel path of `RT_RELAY_HOST` to the relay. Anything else gets 421 or 404. |
| Broker | server | `rootkeel/broker.py`, `rootkeel/store.py` | The owner API, machine enrollment, session grants and their sign-ins, and the relay's admission policy. Proxies terminal traffic to the right machine. Keeps its state in SQLite. |
| Relay | server | frps, `server/config/frps.toml` | Holds each machine's tunnel. Every login, route registration, heartbeat and work connection is checked with the broker first. |
| Agent | each machine | `rootkeel/agent.py`, frpc | Runs as the dedicated `rt-agent` account. Dials the relay over WSS, authenticates every request that comes through the tunnel with the machine credential, and forwards it to Zellij's web server on loopback. Mints and revokes Zellij tokens for grants. |
| Terminal helper | each machine | `rootkeel/terminal.py`, Zellij | Runs as the shared account. Starts the Zellij session and Zellij's web server on `127.0.0.1:8082`, and performs the few Zellij token commands the agent may ask for, over a Unix socket only the agent can use. |
| `rt` | your computer | `rt` | The command-line client: sign-in, enrollment, grants, and `rt attach`, which runs the stock Zellij client through a local adapter. |

## Identities and credentials

- **The owner credential** (`<state>/owner`): 256 random bits from `prepare.sh`. It is the only
  credential for the owner API (`/api/*` on `RT_PUBLIC_HOST`). `rt login` stores it on your
  computer.
- **Machine credentials**: issued once per machine at enrollment, in exchange for a one-use code
  the owner requested. The agent presents its credential at every relay login; the broker stores
  it encrypted.
- **Session grants**: one grant opens one session name on one machine. It has an id (`g_…`) and a
  secret session credential. Exchanging the credential at the grant's sign-in gives a cookie
  scoped to that grant's path. A grant stays open until it is revoked, unless the operator sets a
  time limit.
- **Zellij tokens**: each grant has its own Zellij web token on its machine, minted by the agent
  and kept encrypted by the broker. The browser never sees it.

## Hostnames and origins

| Host | Serves |
|---|---|
| `RT_PUBLIC_HOST` | The owner API, `rt enroll`, and an informational page. Never a terminal. |
| `owner.RT_PUBLIC_HOST` | The terminal pages and their WebSockets, under `/g/<grant>/`. Never the API. |
| `RT_RELAY_HOST` | Only the agents' tunnel upgrade path. |

Keeping the terminal on its own origin means terminal content can never run script against the
API's origin.

## A browser session, step by step

1. `rt grant main` calls `POST /api/grants` with the owner credential. The broker checks that the
   machine has a session `main` (unless `--create` asks for a new one), asks the agent through the
   relay for a fresh Zellij token, stores the grant, and returns
   `https://owner.term.example.com/g/<grant>/main` with a session credential.
2. The browser opens the URL. Without a cookie for the grant it gets the broker's sign-in page.
   The credential, pasted into its form or carried in the URL fragment of an `rt grant --qr` code
   (`#token=…`, never sent to the server and removed from the address bar before signing in), is
   posted to `/g/<grant>/command/login`; the broker logs in to Zellij on the
   machine with the grant's token and sets a `Secure`, `HttpOnly`, `SameSite=Strict` cookie for
   `/g/<grant>/`.
3. Page, assets and WebSockets go through the broker, which checks the cookie, the exact origin and
   the session name, and forwards them through the relay to the agent and on to Zellij.
4. Revoking the grant (`rt revoke`, or a machine revoke) closes every open connection to it within
   half a second, and the broker then revokes the Zellij token on the machine.

`rt attach` does the same with a native client: it issues a grant, runs `zellij attach` against a
loopback adapter that adds the grant's path prefix, reconnects after network drops, and revokes the
grant when you stop it.

## Several machines

Every machine has its own credential, tunnel and Zellij tokens. A grant is bound to one machine;
the broker forwards its traffic only to that machine. When more than one machine is enrolled,
requests that need a machine (`rt grant`, `rt attach`, `rt machine-revoke`) must name one by id or
name; with a single machine it is chosen by default. All machines' terminals are served on the
owner's terminal origin.

## State

The broker's SQLite database (`<state>/db/broker.sqlite`) holds enrollments, machines, relay
logins, grants, sign-in cookies and a metadata-only audit log (30 days). Grant credentials and
cookies are stored only as SHA-256 hashes. Machine credentials, Zellij tokens and Zellij cookies
are sealed with AES-GCM under per-value keys, wrapped by `<state>/db-key`, which is mounted
separately from the database. Zellij sessions and their processes live on the machines; the
server holds no terminal content.
