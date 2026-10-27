# Operations

All commands run on the server from the checkout of the release you run, with your environment
file. `compose.sh` passes everything after the file to `docker compose`.

## Status and logs

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env ps
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env logs --tail 100 broker relay proxy
```

The front door logs only a timestamp and status per request: no paths, cookies or bodies. On a
machine, `journalctl -u rootkeel-agent -u rootkeel-terminal` shows the agent and the terminal
helper.

## Revoking access

From your computer:

```sh
rt grants                      # open grants
rt revoke GRANT                # end one
rt machines                    # enrolled machines
rt machine-revoke laptop       # disable a machine and end all its grants
```

On the server, without the owner credential, `rootkeel.admin` works on the database directly:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env exec broker python3 -m rootkeel.admin machines
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env exec broker python3 -m rootkeel.admin list
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env exec broker python3 -m rootkeel.admin revoke --grant g_...
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env exec broker python3 -m rootkeel.admin revoke --all
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env exec broker python3 -m rootkeel.admin machine-revoke --machine laptop
```

The running broker closes the connections of ended grants within a second. Ending access never
stops a session or its processes; on the machine, `zellij kill-session NAME` (as the shared account)
does.

To remove a machine for good, revoke it, then on the machine:

```sh
sudo systemctl disable --now rootkeel-agent rootkeel-terminal
sudo rm -rf /var/lib/rootkeel /etc/systemd/system/rootkeel-*.service
```

## Rotating the owner credential

Rotate it when a computer that held it is lost, or when anyone else may have seen it. Existing
grants are not affected; revoke them too if they may have leaked.

```sh
state=/srv/rootkeel/prod                         # your RT_STATE_DIR
sudo sh -c "umask 077; openssl rand -hex 32 > $state/owner.new"
sudo chown 65534:65534 "$state/owner.new"
sudo mv "$state/owner.new" "$state/owner"
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env restart broker
```

Then run `rt login` again on each of your computers.

## Backups

Back up `<state>/db/` and `<state>/db-key` together, but store them apart: the database is useless
without the key, and the key is what protects the machine credentials in it. Stop the broker for a
consistent copy, or use SQLite's online backup:

```sh
state=/srv/rootkeel/prod                         # your RT_STATE_DIR
sudo sh -c "umask 077; python3 -c 'import sqlite3, sys; sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))' $state/db/broker.sqlite /root/broker-\$(date +%F).sqlite"
```

Also keep `<state>/owner` (or plan to rotate it after a restore) and your environment file. Never
restore one environment's database and key into another environment.

Losing the database is recoverable without a restore: start with an empty `db/`, then enroll each
machine again (on each machine, rerun `agent/install.sh` with the new `machine.json`; it replaces
the old one).

## Upgrades

1. Read the release notes.
2. Check out the new release. Set `RT_BROKER_IMAGE` and `RT_RELAY_IMAGE` to the image digests its
   release notes print; for a commit that is not a release, build its images under a new version tag
   (an existing tag is refused), for example `scripts/build-images.sh rootkeel-local 0.2.0`, and
   set them to the new references (`rootkeel-local/broker:0.2.0`, `rootkeel-local/relay:0.2.0`).
   Keep `RT_PROXY_IMAGE` as the release's example file pins it.
3. Back up as above, then:

   ```sh
   sudo scripts/prepare.sh /etc/rootkeel/rootkeel.env
   sudo scripts/compose.sh /etc/rootkeel/rootkeel.env up -d
   ```

4. Update `rt` on your computers, and on each machine rerun `sudo agent/install.sh <user>
   /var/lib/rootkeel/machine.json` from the new checkout. Restarting the agent revokes every Zellij
   token on that machine; open browser sessions sign in to Zellij again by themselves, and the
   sessions keep running.

## Certificates

The front door reads the certificate when it starts. After renewing it, restart it:

```sh
sudo scripts/compose.sh /etc/rootkeel/rootkeel.env restart proxy
```

The certificate must keep covering `RT_PUBLIC_HOST`, `owner.RT_PUBLIC_HOST` and `RT_RELAY_HOST`;
`prepare.sh` checks this.
