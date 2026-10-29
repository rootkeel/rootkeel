# Security

Rootkeel gives whoever holds a grant a writable shell on your machine, as the account you shared.
Treat every credential it issues as you would an SSH key to that account. This page describes what
Rootkeel protects, how, and where its protection ends. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).

## Trust model

- **One owner.** The owner credential can enroll and revoke machines, issue and revoke grants and
  read the audit log. Anyone holding it controls every enrolled machine's shared account. Keep it
  only in `<state>/owner` on the server and in `rt login`'s 0600 file on your own computers.
- **The server operator sees everything.** TLS ends at the front door, so the server sees terminal
  traffic in plaintext. Rootkeel is not end-to-end encrypted. Run the server yourself, on a host
  you trust.
- **The shared account is the boundary.** A terminal runs as one ordinary Linux account. Someone
  with a grant can do anything that account can do, including reading its other files and
  processes and every other Zellij session of that account. Rootkeel does not isolate panes or
  sessions from each other. If you [share root](install.md#sharing-root), the account is root, so
  a grant controls the whole machine and can read the machine credential.

## Credentials

| Credential | Lifetime | Stored |
|---|---|---|
| Owner credential | Until you rotate it ([operations.md](operations.md)) | `<state>/owner`; `~/.config/rt/<host>/credential` (0600) |
| Enrollment code | Five minutes, one use | Server: SHA-256 only |
| Machine credential | Until the machine is revoked | Machine: `/var/lib/rootkeel/machine.json` (0600, `rt-agent`); server: AES-GCM sealed |
| Grant session credential | Until the grant is revoked, or an operator time limit | Server: SHA-256 only |
| Grant cookie | As long as the grant; renewed on every page load, browsers keep it up to 400 days | Server: SHA-256 only |
| Zellij token and cookie | Per grant; revoked with the grant, and all of them when the agent restarts | Server: AES-GCM sealed; machine: memory only |

Sealed values use a fresh key each, wrapped with `<state>/db-key` and bound to their table, column
and row, so a copied database without the key, or a value moved to another row, yields nothing.
Keep `db-key` out of the database's backups (back it up separately; see
[operations.md](operations.md)).

A grant is a standing capability: a leaked session credential or grant cookie works until the grant
is revoked. The QR code of `rt grant --qr` holds the session credential too, in the URL fragment,
which browsers never send to the server; anyone who scans or photographs the code can sign in. List
open grants with `rt grants` and revoke the ones you no longer use; at most 32 are open at a time.
`rt attach` revokes its own grant when it exits.

`rt attach` passes the grant's Zellij login token to the stock Zellij client on its command line,
where other processes of the same user (or root) on your computer can see it while it runs. The
long-lived owner credential is never passed that way, and the client runs with a temporary home
directory so no remote-session credentials are saved.

## What the broker enforces

- Only `RT_PUBLIC_HOST` and `owner.RT_PUBLIC_HOST` are served; any other Host is refused. The API
  is never served on the terminal host, and terminals never on the public host.
- Every request with an `Origin` header must come from the exact origin of the host it is sent to.
  Browser-issued cookies additionally require that exact origin on every WebSocket upgrade.
- Cookies are `Secure`, `HttpOnly`, `SameSite=Strict`, host-only and scoped to one grant's path.
- A grant opens only its own session: requests for any other session, or for Zellij's unnamed
  default session, are refused, and the session list is filtered to the granted session.
- Grant sign-ins, enrollment codes and enrollments share one rate limit of 120 a minute per client
  address (an IPv6 client counts as its /64), so one client's flood does not lock out another. The
  client address comes from the front door's `X-Forwarded-For` only on requests from the front door.
- Responses carry HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; the broker's own pages carry a strict
  Content Security Policy.
- Revoking a grant or a machine closes its open connections within half a second, including a
  revoke made by `python3 -m rootkeel.admin` in another process.

The terminal page is Zellij's own, unchanged except for four scripts the broker adds: a guard that
reloads the page when it fails to load during a network change, one that takes an `rt grant --qr`
credential out of the address bar, phone copy and paste, and a full-screen button. None of them
sends anything anywhere.

## What the relay and agent enforce

- The relay admits a machine only with its current credential, and only routes it registered under
  its own fixed name; every heartbeat and work connection is checked against the login that
  started it. A revoked machine's tunnel is refused at its next check.
- The relay's admin and vhost ports, and the broker's relay-policy listener, are reachable only on
  the server's internal network. On Kubernetes the chart's NetworkPolicies provide that network,
  which needs a network plugin that enforces them ([helm.md](helm.md#network-policy)).
- The agent authenticates every request that arrives through the tunnel with the machine
  credential, so a request that reached the relay's private side some other way still cannot use
  the machine.
- The agent runs as `rt-agent`, separate from the shared account, with no core dumps and ptrace
  restricted, so the shared account cannot read the machine credential from the agent's files or
  memory. The terminal helper accepts only the agent, and only a fixed list of Zellij token
  commands.

## What Rootkeel does not do

- It does not stop processes when access ends: revoking a grant closes the connections, and the
  session keeps running. Use `zellij kill-session NAME` on the machine to end a session.
- It does not record terminal content. The audit log records metadata only (who, which grant,
  which machine and session, when) and keeps 30 days.
- Its rate limits are per client address; they do not stop a distributed attack from many addresses. Put the
  server behind network controls of your own if it needs them.
