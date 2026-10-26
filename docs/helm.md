# Install on Kubernetes with Helm

This guide runs one Rootkeel server on a Kubernetes cluster with the Helm chart in
[`deploy/helm/rootkeel`](../deploy/helm/rootkeel), signs `rt` in, and shares a first Linux machine.
The chart runs the same three components as the Compose stack (the nginx front door, the broker and
the relay) from the same images and configuration; only the way it is installed differs. The steps
after the server is running are the same as in [install.md](install.md).

## What you need

| Where | What |
|---|---|
| Cluster | Kubernetes 1.27 or later, with a default storage class (or a PersistentVolumeClaim you create) and a way to expose a Service: a load balancer or node ports. A network plugin that enforces NetworkPolicy (most do); see [Network policy](#network-policy). |
| Your workstation | `kubectl` and Helm 3, with access to the cluster; a checkout of the release you run. |
| Registry | One the cluster can pull from, for the broker and relay images until release images are published. |
| DNS | Three names pointing at the front door's external address, for example `term.example.com`, `owner.term.example.com` and `term-relay.example.com`. |
| Certificate | One TLS certificate covering all three names, as a Kubernetes TLS Secret, or issued by [cert-manager](https://cert-manager.io). |
| Machines and your computer | As in [install.md](install.md#what-you-need). |

## 1. Build and push the images

Release images are not published yet. Build the broker and relay from the checkout and push them to
your registry:

```sh
git clone https://github.com/rootkeel/rootkeel.git && cd rootkeel
git checkout v0.1.0            # the release you run
scripts/build-images.sh --push registry.example.com/rootkeel 0.1.0-local-$(git rev-parse --short=12 HEAD)
```

It prints the two image references; use them below. A private registry needs an image pull Secret
in the namespace, named in `images.pullSecrets`.

## 2. Write your values

Every setting of the Compose environment file has a chart value. Start from the defaults in
[`values.yaml`](../deploy/helm/rootkeel/values.yaml) and write only what you change, for example to
`rootkeel-values.yaml`:

```yaml
environment: prod
publicHost: term.example.com
relayHost: term-relay.example.com
images:
  broker: registry.example.com/rootkeel/broker:0.1.0-local-0123456789ab
  relay: registry.example.com/rootkeel/relay:0.1.0-local-0123456789ab
```

| Environment file | Chart value | Notes |
|---|---|---|
| `RT_ENVIRONMENT` | `environment` | |
| `RT_PUBLIC_HOST`, `RT_RELAY_HOST` | `publicHost`, `relayHost` | |
| `RT_PUBLIC_PORT` | `publicPort` | Also the port of the front door's Service. |
| `RT_HTTPS_BIND` | `service` | How the front door is reached; see step 4. |
| `RT_STATE_DIR` | `persistence` | A PersistentVolumeClaim instead of a host directory. |
| `RT_TLS_CERT_FILE`, `RT_TLS_KEY_FILE` | `tls.secretName` | A Kubernetes TLS Secret, or `tls.certManager`. |
| `RT_HSTS_MAX_AGE` | `hstsMaxAge` | |
| `RT_GRANT_IDLE_TIMEOUT`, `RT_GRANT_MAX_LIFETIME` | `grantIdleTimeout`, `grantMaxLifetime` | |
| `RT_BROKER_IMAGE`, `RT_RELAY_IMAGE`, `RT_PROXY_IMAGE` | `images.broker`, `images.relay`, `images.proxy` | Keep `images.proxy` as the release pins it. |
| `RT_REQUIRE_IMAGE_DIGEST` | `requireImageDigest` | |

The chart checks values with the same rules as `rootkeel.config check-env`: a bad hostname, a moving
image tag or an out-of-range limit fails `helm install` with a message naming the value. No value
holds a credential.

## 3. The certificate

Create a TLS Secret holding the full certificate chain and its key, in the namespace you install
into. Its default name is `<release>-tls` (`rootkeel-tls` for a release named `rootkeel`); set
`tls.secretName` to use another:

```sh
kubectl create namespace rootkeel
kubectl -n rootkeel create secret tls rootkeel-tls --cert=fullchain.pem --key=privkey.pem
```

Or let cert-manager issue it: with an issuer already set up in the cluster, add

```yaml
tls:
  certManager:
    enabled: true
    issuerRef:
      name: letsencrypt        # your Issuer or ClusterIssuer
      kind: ClusterIssuer
```

and the chart creates a Certificate for the three names. The front door starts once the Secret
exists. An HTTP-01 challenge needs the three names to reach a solver over port 80, which the front
door does not serve; use a DNS-01 issuer.

## 4. Choose how the front door is reached

The front door terminates TLS itself, and both the browsers' HTTPS and the machines' tunnel arrive
on its one port, so exactly one port has to be reachable from the Internet.

- **LoadBalancer (the default).** The Service `<release>-proxy` gets its own external address on
  port `publicPort` (443). `service.externalTrafficPolicy: Local` (the default) keeps the client's
  address, which the broker uses for its per-client sign-in rate limit. Set
  `service.annotations`, `service.loadBalancerIP` or `service.loadBalancerSourceRanges` as your
  cloud requires.
- **NodePort.** Set `service.type: NodePort` and, optionally, `service.nodePort`. Clients then use
  that port: set `publicPort` to the same value, unless something in front of the nodes maps 443
  to it.
- **ClusterIP.** Set `service.type: ClusterIP` to expose the Service `<release>-proxy` yourself.

## 5. Install

```sh
helm install rootkeel deploy/helm/rootkeel -n rootkeel -f rootkeel-values.yaml
kubectl -n rootkeel get pods
```

On first start an init step of the broker writes the owner credential and the database encryption
key to the state volume, as `scripts/prepare.sh` does for Compose. It never replaces them, so
upgrades and restarts keep them. Read the owner credential for `rt login`:

```sh
kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- cat /state/owner
```

Point the three DNS names at the front door's address (`kubectl -n rootkeel get service
rootkeel-proxy`), then check it:

```sh
curl -sS https://term.example.com/ | grep -o '<title>.*</title>'
```

Then continue with [install.md, step 3](install.md#3-sign-rt-in): sign `rt` in, enroll a machine
and open a terminal.

## What the chart creates

| Resource | Purpose |
|---|---|
| Deployment and Service `<release>-proxy` | The nginx front door; the Service is the way in. Scale it with `proxy.replicas`. |
| Service `<release>-proxy-pods` | Headless: lets the broker recognise the front door's own addresses, so it trusts `X-Forwarded-For` only from them. Not a way in. |
| Deployment and Service `<release>-broker` | The broker, one replica; its Service is internal only. |
| Deployment and Service `<release>-relay` | The relay (frps), one replica; its Service is internal only. |
| PersistentVolumeClaim `<release>-state` | `owner`, `db-key` and `db/broker.sqlite`. Kept when the release is uninstalled (`persistence.keep`). |
| ConfigMaps | nginx's and frps' configuration, from `server/config` with the Service names filled in. |
| NetworkPolicies | See below. |
| Certificate | Only with `tls.certManager.enabled`. |

The broker and the relay always run one replica each: the broker keeps its state in one SQLite
database, and the relay holds every machine's tunnel. Both are replaced, not rolled, on upgrade, so
machines reconnect within seconds and open terminals reconnect by themselves. Every container runs
with a read-only root filesystem and no added privileges; the broker and relay run as uid 65534. The
front door runs the stock nginx image, whose master process needs root (with only the capabilities
to bind 443 and switch to its worker user), so the namespace must allow the `baseline` Pod Security
level.

## Network policy

In Compose, the broker and the relay sit on a private network that nothing else can reach. The
broker's relay-policy listener relies on that: any client that can reach it can admit machines to
the relay. The chart's NetworkPolicies, always created, recreate that
network: the broker and the relay accept connections only from each other and from the front door,
and open none except to each other and DNS. They take effect only when the cluster's network plugin
enforces NetworkPolicy; on a cluster whose plugin does not, run Rootkeel in a cluster or node pool
you do not share.

## Operations

The commands of [operations.md](operations.md) work the same way through `kubectl exec`. For
example:

```sh
kubectl -n rootkeel logs deploy/rootkeel-broker
kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- python3 -m rootkeel.admin machines
kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- python3 -m rootkeel.admin revoke --all
```

- **Rotating the owner credential.** Write a new one in place and restart the broker:

  ```sh
  kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- python3 -c \
    'import os, secrets; os.umask(0o077); open("/state/owner.new", "w").write(secrets.token_hex(32) + "\n"); os.replace("/state/owner.new", "/state/owner")'
  kubectl -n rootkeel rollout restart deploy/rootkeel-broker
  ```

  Then run `rt login` again on each of your computers.
- **Backups.** Back up `db/` and `db-key` from the state volume together, but store them apart, as
  described in [operations.md](operations.md#backups): with your storage's volume snapshots, or
  with SQLite's online backup in the running broker:

  ```sh
  kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- python3 -c \
    'import sqlite3; sqlite3.connect("/state/db/broker.sqlite").backup(sqlite3.connect("/tmp/broker.sqlite"))'
  kubectl -n rootkeel cp -c broker "$(kubectl -n rootkeel get pod -l app.kubernetes.io/component=broker -o name | cut -d/ -f2)":/tmp/broker.sqlite broker-$(date +%F).sqlite
  kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- rm /tmp/broker.sqlite
  ```
- **Certificates.** The front door reads the certificate when it starts. After the Secret is
  renewed, by you or by cert-manager, restart it: `kubectl -n rootkeel rollout restart
  deploy/rootkeel-proxy`.
- **Upgrades.** Build and push the new release's images under a new version tag, set them in your
  values and run `helm upgrade rootkeel deploy/helm/rootkeel -n rootkeel -f rootkeel-values.yaml`
  from the new checkout. Update `rt` and the agents as in [operations.md](operations.md#upgrades).
- **Uninstalling.** `helm uninstall rootkeel -n rootkeel` keeps the state claim; delete it with
  `kubectl -n rootkeel delete pvc rootkeel-state` once you no longer need the credential and the
  database.
