# Configuration

## The environment file

The server reads one environment file (start from [`server/example.env`](../server/example.env)).
`scripts/prepare.sh` and `scripts/compose.sh` validate it with `python3 -m rootkeel.config
check-env <file>` and refuse to continue on any problem, listing every problem at once. Values are
literal `KEY=VALUE` lines; `$` interpolation is refused, and Compose sees only the values in the
file, never same-named shell variables.

On Kubernetes the Helm chart takes the same settings as chart values;
[helm.md](helm.md#2-write-your-values) maps each one to its value.

| Setting | Required | Meaning |
|---|---|---|
| `RT_ENVIRONMENT` | yes | A short lowercase name (letters, digits, hyphens; at most 30). Names the Compose project `rootkeel-<name>` and labels machine credentials. |
| `RT_PUBLIC_HOST` | yes | The DNS name of the API and of `rt`. The owner's terminal pages are served on `owner.<RT_PUBLIC_HOST>`. |
| `RT_RELAY_HOST` | yes | The DNS name the agents dial. Must differ from `RT_PUBLIC_HOST` and `owner.<RT_PUBLIC_HOST>`. |
| `RT_PUBLIC_PORT` | no | The HTTPS port clients use for both names. Default 443. |
| `RT_HTTPS_BIND` | yes | The `address:port` the front door binds on this host, for example `0.0.0.0:443`. |
| `RT_STATE_DIR` | yes | Absolute path of the server's private state, outside the checkout. |
| `RT_TLS_CERT_FILE`, `RT_TLS_KEY_FILE` | no | Absolute paths of the certificate chain and key. Default `<state>/certs/server.crt` and `server.key`. The certificate must cover `RT_PUBLIC_HOST`, `owner.RT_PUBLIC_HOST` and `RT_RELAY_HOST`. |
| `RT_HSTS_MAX_AGE` | no | Strict-Transport-Security max-age in seconds, 0 to 63072000. Default 31536000. |
| `RT_GRANT_IDLE_TIMEOUT` | no | End a grant this many seconds after its last terminal input. 0 (the default) means never; otherwise at least 60. |
| `RT_GRANT_MAX_LIFETIME` | no | End a grant this many seconds after it was issued. 0 (the default) means never; otherwise at least 60. |
| `RT_BROKER_IMAGE`, `RT_RELAY_IMAGE`, `RT_PROXY_IMAGE` | yes | Image references pinned to one build: a version tag or a digest. Moving tags such as `latest`, `main` or `stable` are refused. |
| `RT_REQUIRE_IMAGE_DIGEST` | no | `true` to accept only references pinned by digest (`…@sha256:<64 hex>`). Default `false`. |
| `RT_DOCKER_CONFIG_DIR` | no | Environment variable, not a file setting: where `compose.sh` keeps the Compose and Buildx plugins it fetches when the host lacks them. Default `<state>/docker-config`. |

By default nothing ends a grant but a revoke: a terminal is meant to stay open. The two grant limits
are administrative policy for operators who must bound access anyway; they apply to every grant,
including grants issued before the limit was set.

## The state directory

| Path | Contents |
|---|---|
| `owner` | The owner credential (mode 0600, owned by uid 65534). |
| `db-key` | The 32-byte database encryption key (mode 0600). Kept outside `db/`. |
| `db/` | The broker's SQLite database. |
| `certs/` | The default certificate location, and the lab CA from `prepare.sh --lab-ca`. |

## The broker process

Compose sets these for the broker container; set them yourself only when you run the broker some
other way.

| Variable | Meaning |
|---|---|
| `RT_ENVIRONMENT`, `RT_PUBLIC_HOST`, `RT_PUBLIC_PORT`, `RT_RELAY_HOST`, `RT_HSTS_MAX_AGE`, `RT_GRANT_*` | As in the environment file. |
| `RT_OWNER_FILE` | The owner credential file (at least 32 characters). |
| `RT_DB` | The SQLite database path. |
| `RT_DB_KEY_FILE` | The encryption key file; must be outside the database's directory. |
| `RT_RELAY_VHOST_URL` | The relay's private HTTP vhost listener, for example `http://relay:8080`. |
| `RT_FRONT_DOOR_HOST` | The front door's hostname (`proxy` in Compose). Only requests from its addresses may name the client in `X-Forwarded-For`, for the per-client authentication rate limit; unset, the peer address is the client. |

The broker listens on port 8080 (behind the front door) and on port 8081 for the relay's policy
calls only. Neither port is published by the Compose stack.

## The agent

`agent/install.sh` writes two systemd units. Their settings:

| Variable | Unit | Meaning |
|---|---|---|
| `RT_MACHINE_FILE` | agent | The machine credential, `/var/lib/rootkeel/machine.json`. |
| `RT_FRPC_CONFIG` | agent | Where the agent writes frpc's configuration. Default next to `RT_MACHINE_FILE`. |
| `RT_TERMINAL_SOCKET` | both | The helper's Unix socket, `/var/lib/rootkeel-helper/terminal.sock`, or `@name` for an abstract socket. |
| `RT_TERMINAL_PEER_UID` | terminal | Admit only this uid (the agent's) to the helper socket. Required for an abstract socket. |
| `RT_SESSION` | both | The session started at boot. `main` unless the installer was run with `RT_SESSION`. |
| `RT_ZELLIJ_CONFIG` | terminal | Zellij's configuration directory. Default `agent/zellij` of the installed copy. |

`machine.json` comes from `rt enroll` and names the one relay the machine may dial. Running the
installer again with a new `machine.json` replaces the old one; revoke the old machine on its server.

## rt

| Option | Environment | Meaning |
|---|---|---|
| `--url` | `RT_URL` | The server's HTTPS origin. Default: the one `rt login` saved. |
| `--credential-file` | `RT_CREDENTIAL_FILE` | Read the owner credential from this file instead of the one `rt login` saved. |
| `--ca-cert` | `RT_CA_CERT` | Trust this CA certificate (for a lab CA). |
| `--machine` | `RT_MACHINE` | The machine, by id or name, when several are enrolled. |
| `--create` | | `attach`, `grant`: start a new session with this name if the machine has none. |
| `--name` | | `enrollment`: a name for the machine to enroll. |
| `--zellij` | | The Zellij executable for `rt attach`. Default `zellij`. |

Commands: `login`, `logout`, `machines`, `enrollment`, `enroll FILE`, `grant SESSION`, `grants`,
`revoke GRANT`, `attach SESSION`, `machine-revoke [MACHINE]`, `audit`.
