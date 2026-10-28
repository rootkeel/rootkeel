# Install on Kubernetes with Helm

This guide runs one Rootkeel server on a Kubernetes cluster with the Helm chart in
[`deploy/helm/rootkeel`](../deploy/helm/rootkeel). The chart runs the same three components as the
Compose stack (the nginx front door, the broker and the relay) from the same images and
configuration; only the way they are installed differs. Once the server answers, you install `rt`,
sign in and share machines exactly as in [install.md](install.md), from
[step 9](install.md#9-install-rt-on-your-computer) on.

Read the introduction of [install.md](install.md#how-the-pieces-fit) first if you are new to
Rootkeel: it explains the roles of the server, your computer and the machines to share. Here,
**your workstation** is the computer that runs `kubectl` and `helm` against the cluster; it can be
the same as your computer. This guide uses the same example values as install.md
(`term.example.com`, `owner.term.example.com`, `term-relay.example.com`), a namespace `rootkeel`
and a Helm release named `rootkeel`.

## 1. Check the prerequisites

**On your workstation**:

| Need | Check with | Expect |
|---|---|---|
| `kubectl`, configured for the cluster | `kubectl version` | A `Client Version` and a `Server Version` line, no error |
| Kubernetes 1.27 or later (the chart refuses older clusters) | the `Server Version` above | `v1.27.0` or later |
| Helm 3 | `helm version --short` | `v3.…` |
| Git | `git --version` | `git version 2.…` |
| A default storage class, for the 1 GiB state volume | `kubectl get storageclass` | One class marked `(default)`, or plan to set `persistence.storageClass` or `persistence.existingClaim` |
| A way to expose a Service to the Internet: a load balancer (most clouds) or reachable node ports | your cluster's documentation | |
| A network plugin that enforces NetworkPolicy | your cluster's documentation | See [Network policy](#network-policy) |
| Namespaces may run Pod Security level `baseline` (the front door's nginx master process starts as root) | `kubectl get ns rootkeel --show-labels` after step 3 | No `pod-security.kubernetes.io/enforce=restricted` label |

You also need the DNS zone for your hostnames and, for the certificate, either a certificate you
can obtain yourself or [cert-manager](https://cert-manager.io) in the cluster with a DNS-01 issuer.

## 2. Get the chart

The chart is in the repository, not in a chart registry. **On your workstation**, clone the
repository and check out the release you install:

```sh
git clone https://github.com/rootkeel/rootkeel.git
cd rootkeel
git checkout v0.1.0
```

All remaining commands run from this directory.

## 3. Create the namespace

```sh
kubectl create namespace rootkeel
```

prints `namespace/rootkeel created`. If your cluster enforces Pod Security level `restricted` on new
namespaces by default, allow `baseline` for this one:

```sh
kubectl label namespace rootkeel pod-security.kubernetes.io/enforce=baseline --overwrite
```

## 4. Provide the TLS certificate

The front door terminates TLS itself and needs one certificate covering all three names:
`term.example.com`, `owner.term.example.com` and `term-relay.example.com`. It reads it from a
Kubernetes TLS Secret, by default named `<release>-tls`: `rootkeel-tls` here. Set
`tls.secretName` in step 5 to use another name. The front door pods wait (in `ContainerCreating`)
until the Secret exists.

### Option A: a certificate you have

With the full chain in `fullchain.pem` (your certificate first, then the intermediates) and its
unencrypted key in `privkey.pem`, for example from certbot as in
[install.md, step 5](install.md#5-get-a-tls-certificate) or from your certificate authority:

```sh
kubectl -n rootkeel create secret tls rootkeel-tls --cert=fullchain.pem --key=privkey.pem
```

prints `secret/rootkeel-tls created`. To renew, replace the Secret and restart the front door:

```sh
kubectl -n rootkeel create secret tls rootkeel-tls --cert=fullchain.pem --key=privkey.pem \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n rootkeel rollout restart deploy/rootkeel-proxy
```

### Option B: cert-manager

With cert-manager installed and an issuer already set up, add this to your values in step 5:

```yaml
tls:
  certManager:
    enabled: true
    issuerRef:
      name: letsencrypt        # your Issuer or ClusterIssuer
      kind: ClusterIssuer
```

The chart then creates a Certificate for the three names, and cert-manager writes the Secret. Use
a DNS-01 issuer: an HTTP-01 challenge needs the names to reach a solver over port 80, which the
front door does not serve. After each renewal, restart the front door (`kubectl -n rootkeel rollout
restart deploy/rootkeel-proxy`): it reads the certificate only when it starts.

### Check the Secret

```sh
kubectl -n rootkeel get secret rootkeel-tls -o jsonpath='{.data.tls\.crt}' | base64 -d \
  | openssl x509 -noout -ext subjectAltName -enddate
```

The output lists all three names and the expiry date.

## 5. Write your values

Every setting of the Compose settings file has a chart value. Write only what you change, to a
file on your workstation such as `rootkeel-values.yaml`:

```yaml
environment: prod
publicHost: term.example.com
relayHost: term-relay.example.com
```

The chart defaults to the published 0.1.0 images, pinned by digest, with `requireImageDigest:
true`. For another release, set `images.broker` and `images.relay` to the digests it prints under
"Server images" in its notes on the [releases page](https://github.com/rootkeel/rootkeel/releases).
The images are public, so no pull Secret is needed. The defaults for everything else are in
[`values.yaml`](../deploy/helm/rootkeel/values.yaml). No value holds a credential: the owner
credential and the database key are generated inside the cluster, on the state volume.

| Settings file | Chart value | Notes |
|---|---|---|
| `RT_ENVIRONMENT` | `environment` | |
| `RT_PUBLIC_HOST`, `RT_RELAY_HOST` | `publicHost`, `relayHost` | |
| `RT_PUBLIC_PORT` | `publicPort` | Also the port of the front door's Service. Default 443. |
| `RT_HTTPS_BIND` | `service` | How the front door is reached; see step 6. |
| `RT_STATE_DIR` | `persistence` | A PersistentVolumeClaim instead of a host directory. |
| `RT_TLS_CERT_FILE`, `RT_TLS_KEY_FILE` | `tls.secretName` | A Kubernetes TLS Secret, or `tls.certManager`. |
| `RT_HSTS_MAX_AGE` | `hstsMaxAge` | |
| `RT_GRANT_IDLE_TIMEOUT`, `RT_GRANT_MAX_LIFETIME` | `grantIdleTimeout`, `grantMaxLifetime` | |
| `RT_BROKER_IMAGE`, `RT_RELAY_IMAGE`, `RT_PROXY_IMAGE` | `images.broker`, `images.relay`, `images.proxy` | Keep `images.proxy` as the release pins it. |
| `RT_REQUIRE_IMAGE_DIGEST` | `requireImageDigest` | |

The chart checks values with the same rules as `rootkeel.config check-env`: a bad hostname, a moving
image tag or an out-of-range limit fails `helm install` with a message naming the value. To see
the result without installing, run
`helm template rootkeel deploy/helm/rootkeel -n rootkeel -f rootkeel-values.yaml`.

### Building the images yourself

For a commit that is not a release, build the broker and relay from the checkout and push them to
a registry the cluster can pull from (this needs Docker on your workstation):

```sh
scripts/build-images.sh --push registry.example.com/rootkeel 0.1.0-local-$(git rev-parse --short=12 HEAD)
```

It prints the two image references with their digests; put those into `images.broker` and
`images.relay`. A private registry needs an image pull Secret in the namespace, named in
`images.pullSecrets`.

## 6. Choose how the front door is reached

Browsers' HTTPS and the machines' tunnel both arrive on the front door's one port, so exactly one
TCP port has to be reachable from the Internet.

- **LoadBalancer (the default).** The Service `rootkeel-proxy` gets its own external address on
  TCP port `publicPort` (443). `service.externalTrafficPolicy: Local` (the default) keeps the
  client's address, which the broker uses for its per-client sign-in rate limit. Set
  `service.annotations`, `service.loadBalancerIP` or `service.loadBalancerSourceRanges` as your
  cloud requires. Make sure the cloud firewall allows inbound TCP 443 to the load balancer.
- **NodePort.** Set `service.type: NodePort` and, optionally, `service.nodePort` (for example
  `30443`). Clients then use that port: set `publicPort` to the same value, unless something in
  front of the nodes maps 443 to it. Open that TCP port on the nodes' firewall.
- **ClusterIP.** Set `service.type: ClusterIP` and expose the Service `rootkeel-proxy` yourself.
  Whatever you put in front must pass TLS through untouched (no TLS termination), on TCP.

## 7. Install

**On your workstation**:

```sh
helm install rootkeel deploy/helm/rootkeel -n rootkeel -f rootkeel-values.yaml
```

It prints `STATUS: deployed` and notes with the next steps. Wait for the pods:

```sh
for name in broker relay proxy; do kubectl -n rootkeel rollout status deploy/rootkeel-$name; done
kubectl -n rootkeel get pods
```

Each of the three lines ends with `successfully rolled out`, and every pod shows `1/1` `Running`.
On its first start, an init step of the broker writes the owner credential and the database
encryption key to the state volume, as `scripts/prepare.sh` does for Compose; it never replaces
them, so upgrades and restarts keep them. A pod that stays `Pending` or `ContainerCreating` is
covered in [Troubleshooting](#troubleshooting).

## 8. Point DNS at the front door and check it

Find the front door's external address:

```sh
kubectl -n rootkeel get service rootkeel-proxy
```

With a load balancer, the `EXTERNAL-IP` column shows an IP address or a hostname (it says
`<pending>` for a minute or two while the cloud creates it). Create three DNS records:

| Record name | If `EXTERNAL-IP` is an IP address | If it is a hostname |
|---|---|---|
| `term.example.com` | `A` record to that address | `CNAME` record to that hostname |
| `owner.term.example.com` | `A` record to that address | `CNAME` record to that hostname |
| `term-relay.example.com` | `A` record to that address | `CNAME` record to that hostname |

With node ports, point the records at a node's public address (or at whatever you put in front).
Check the records and the server **from your computer** as in
[install.md, step 2](install.md#2-create-the-dns-records) and
[step 8](install.md#check-that-it-is-healthy):

```sh
curl -sS https://term.example.com/ | grep -o '<title>.*</title>'
```

prints `<title>Rootkeel sign-in</title>` (add `:<publicPort>` to the URL when it is not 443).

## 9. Read the owner credential

```sh
kubectl -n rootkeel exec deploy/rootkeel-broker -c broker -- cat /state/owner
```

prints 64 hexadecimal characters: the owner credential for `rt login`.

Then continue with [install.md, step 9](install.md#9-install-rt-on-your-computer): install `rt`,
sign in with this credential, share a machine and open a terminal.

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

- **Stopping and starting.** Scale the three Deployments to zero and back:
  `kubectl -n rootkeel scale deploy/rootkeel-proxy deploy/rootkeel-broker deploy/rootkeel-relay --replicas=0`,
  then `--replicas=1` (or your `proxy.replicas`). The state volume stays.
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
- **Upgrades.** Read the release notes, back up, then check out the new release in your clone
  (`git fetch --tags && git checkout v0.2.0`), set `images.broker` and `images.relay` to its image
  digests and `images.proxy` to its `values.yaml` default if you overrode it, and run
  `helm upgrade rootkeel deploy/helm/rootkeel -n rootkeel -f rootkeel-values.yaml`. Update `rt` and
  the agents as in [install.md](install.md#upgrade).
- **Uninstalling.** `helm uninstall rootkeel -n rootkeel` keeps the state claim; delete it with
  `kubectl -n rootkeel delete pvc rootkeel-state` once you no longer need the credential and the
  database, then `kubectl delete namespace rootkeel` and remove the three DNS records.

## Troubleshooting

- **`helm install` fails with `Error: execution error at …`** naming a value, for example
  `relayHost must differ from publicHost`: fix that value. `chart requires kubeVersion: >=1.27.0-0`
  means the cluster is too old.
- **The proxy pods stay `ContainerCreating`.** `kubectl -n rootkeel describe pod -l
  app.kubernetes.io/component=proxy` shows `secret "rootkeel-tls" not found`: create the Secret
  (step 4), or check that `tls.secretName` matches its name. With cert-manager,
  `kubectl -n rootkeel describe certificate rootkeel` shows why it is not issued yet.
- **The broker pod stays `Pending`.** `kubectl -n rootkeel get pvc` shows `rootkeel-state` as
  `Pending`: the cluster has no default storage class. Set `persistence.storageClass` or
  `persistence.existingClaim` and run `helm upgrade` with the same values.
- **Pods are refused** with `violates PodSecurity "restricted"`: allow `baseline` on the namespace
  (step 3).
- **`EXTERNAL-IP` stays `<pending>`.** The cluster has no load balancer: use `service.type:
  NodePort` (step 6).
- **DNS, certificate, port or credential problems** look the same as with Compose; see
  [install.md, Troubleshooting](install.md#troubleshooting), using
  `kubectl -n rootkeel logs deploy/rootkeel-proxy` in place of the Compose logs.
