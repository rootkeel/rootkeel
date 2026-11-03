# Install on a cloud VM with Ansible (draft)

> **Draft.** These playbooks have not yet been run against real AWS, Google Cloud, Azure, Hetzner
> Cloud or DigitalOcean accounts. The install part has been run against a disposable Debian 13
> host; the provider parts have only been checked against each collection's documented options.
> Read what they create before you use them, and expect to adjust provider settings such as
> machine types and image names.

The Ansible content in [`deploy/ansible`](../deploy/ansible) creates one VM on a cloud provider
and installs the Rootkeel server on it the same way as [install.md](install.md): it clones the
release to `/opt/rootkeel-server`, gets a TLS certificate, writes `/etc/rootkeel/rootkeel.env`
from the release's `server/example.env`, runs `scripts/prepare.sh` and starts the Compose stack
with `scripts/compose.sh`. In other words, it does steps 4 to 8 of install.md, plus the VM and
its firewall (step 3). You still create the DNS records (step 2), and then install `rt`, sign in
and share machines from [step 9](install.md#9-install-rt-on-your-computer) on.

Read the introduction of [install.md](install.md#how-the-pieces-fit) first if you are new to
Rootkeel. Here, **your workstation** is the computer that runs Ansible. This guide uses the same
example values as install.md (`term.example.com`, `owner.term.example.com`,
`term-relay.example.com`).

## What is in deploy/ansible

| File | What it does |
|---|---|
| `site.yml` | Runs `provision.yml`, then `install.yml`: one command from nothing to a running server. |
| `provision.yml` | Creates the VM and its firewall on one provider (role `rootkeel_cloud_vm`). |
| `install.yml` | Installs the server on the hosts in the `rootkeel_server` group (role `rootkeel_server`): the VM just created. |
| `teardown.yml` | Deletes the VM, all its data and everything `provision.yml` created for it. |
| `requirements.yml` | The providers' Ansible collections, pinned. |

Each provider gets the same thing: a firewall that lets in TCP 22 (SSH), TCP 443 and, for
certbot, TCP 80; an SSH key from your workstation; and one Debian 13 VM with a public IPv4
address. Everything is named after `rootkeel_vm_name` (default `rootkeel`).

| Provider | `rootkeel_cloud` | Collection | Default size and place | Created |
|---|---|---|---|---|
| AWS | `aws` | `amazon.aws` | `t3.small` in `AWS_REGION` (or `us-east-1`) | Key pair, security group in the default VPC, instance |
| Google Cloud | `gcp` | `google.cloud` | `e2-small` in `us-central1-a` | Two firewall rules on the default network, instance |
| Azure | `azure` | `azure.azcollection` | `Standard_B2s` in `eastus` | A resource group holding the network, address, security group and VM |
| Hetzner Cloud | `hetzner` | `hetzner.hcloud` | `cx23` in `nbg1` | SSH key, firewall, server |
| DigitalOcean | `digitalocean` | `digitalocean.cloud` | `s-1vcpu-2gb` in `nyc3` | SSH key, cloud firewall, Droplet |

## 1. Prepare your workstation

| Need | Check with | Expect |
|---|---|---|
| ansible-core 2.19 or later | `ansible --version` | `ansible [core 2.19…]` or later |
| The collections | `ansible-galaxy collection install -r requirements.yml`, from `deploy/ansible` | Each collection installed |
| An SSH key pair | `ls ~/.ssh/id_ed25519.pub` | The file exists. Otherwise create one with `ssh-keygen -t ed25519`, or set `rootkeel_ssh_public_key_file`. |

Then the Python libraries and credentials of the provider you use. The playbooks never take
credentials as settings: each collection reads them from its usual environment variables or
files.

| Provider | Python libraries (`pip install …`) | Credentials |
|---|---|---|
| AWS | `boto3 botocore` | `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; `AWS_REGION` for the region. Or `~/.aws/credentials`. |
| Google Cloud | `google-auth requests` | `GCP_PROJECT` for the project, and either `gcloud auth application-default login` (the default, `GCP_AUTH_KIND=application`) or `GCP_AUTH_KIND=serviceaccount` with `GCP_SERVICE_ACCOUNT_FILE` |
| Azure | `-r` the collection's `requirements.txt` (under `ansible_collections/azure/azcollection/` in your collections path) | `az login`, or `AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `AZURE_SECRET` and `AZURE_TENANT`, or `~/.azure/credentials` |
| Hetzner Cloud | `requests python-dateutil` | `HCLOUD_TOKEN` |
| DigitalOcean | `pydo azure-core` | `DIGITALOCEAN_TOKEN` |

## 2. Choose the settings

Pass settings with `-e name=value`, or put them in a YAML file and pass `-e @settings.yml`.

| Setting | Default | Meaning |
|---|---|---|
| `rootkeel_cloud` | (required) | `aws`, `gcp`, `azure`, `hetzner` or `digitalocean`. |
| `rootkeel_public_host` | (required) | `RT_PUBLIC_HOST`, for example `term.example.com`. |
| `rootkeel_relay_host` | (required) | `RT_RELAY_HOST`, for example `term-relay.example.com`. |
| `rootkeel_tls_mode` | `letsencrypt` | `letsencrypt` (certbot, as in step 5 option A), `files` (a certificate you have, option B) or `lab-ca` (a throwaway CA, for private trials only). |
| `rootkeel_letsencrypt_email` | (required for `letsencrypt`) | The address Let's Encrypt sends expiry notices to. |
| `rootkeel_tls_cert_src`, `rootkeel_tls_key_src` | (required for `files`) | Paths on your workstation to the full chain and the unencrypted key. |
| `rootkeel_version` | `v0.1.3` | The release tag to install. |
| `rootkeel_vm_name` | `rootkeel` | The VM's name; its other resources are named after it. |
| `rootkeel_ssh_source_cidrs` | `[0.0.0.0/0]` | Who may reach SSH. Narrow it to your own address where you can. |
| `rootkeel_ssh_public_key_file` | `~/.ssh/id_ed25519.pub` | The key installed on the VM. Set `rootkeel_ssh_private_key_file` too if ssh does not find the private key by itself. |
| `rootkeel_dns_wait_seconds` | `900` | With `letsencrypt`, how long to wait for each DNS record to point at the VM on its zone's nameservers. |
| `rootkeel_public_ip` | The address Ansible connects to | The address the DNS records must point at. Set it to the public IPv4 address when Ansible reaches the host by hostname or through NAT. |

The other server settings (`rootkeel_environment`, `rootkeel_public_port`, `rootkeel_https_bind`,
`rootkeel_state_dir`, `rootkeel_hsts_max_age`) default to the values in
install.md step 6 and are listed with comments in
[`roles/rootkeel_server/defaults/main.yml`](../deploy/ansible/roles/rootkeel_server/defaults/main.yml).
The provider settings (region, zone, location, machine type, image, resource group) are in
[`roles/rootkeel_cloud_vm/defaults/main.yml`](../deploy/ansible/roles/rootkeel_cloud_vm/defaults/main.yml).

## 3. Create the VM and install the server

Run every command **on your workstation**, from `deploy/ansible`. One example per provider,
with the credentials from step 1 already in the environment:

```sh
# AWS
ansible-playbook site.yml -e rootkeel_cloud=aws \
  -e rootkeel_public_host=term.example.com -e rootkeel_relay_host=term-relay.example.com \
  -e rootkeel_letsencrypt_email=admin@example.com

# Google Cloud
ansible-playbook site.yml -e rootkeel_cloud=gcp -e rootkeel_gcp_zone=europe-west3-a \
  -e rootkeel_public_host=term.example.com -e rootkeel_relay_host=term-relay.example.com \
  -e rootkeel_letsencrypt_email=admin@example.com

# Azure
ansible-playbook site.yml -e rootkeel_cloud=azure -e rootkeel_azure_location=westeurope \
  -e rootkeel_public_host=term.example.com -e rootkeel_relay_host=term-relay.example.com \
  -e rootkeel_letsencrypt_email=admin@example.com

# Hetzner Cloud
ansible-playbook site.yml -e rootkeel_cloud=hetzner \
  -e rootkeel_public_host=term.example.com -e rootkeel_relay_host=term-relay.example.com \
  -e rootkeel_letsencrypt_email=admin@example.com

# DigitalOcean
ansible-playbook site.yml -e rootkeel_cloud=digitalocean -e rootkeel_digitalocean_region=fra1 \
  -e rootkeel_public_host=term.example.com -e rootkeel_relay_host=term-relay.example.com \
  -e rootkeel_letsencrypt_email=admin@example.com
```

A new VM gets a new address, so the DNS records cannot exist before it does. With
`letsencrypt`, the run prints the VM's address and the three records it needs, for example:

```
"Create these A records, each pointing at 203.0.113.10, if they do not exist yet:",
"term.example.com",
"owner.term.example.com",
"term-relay.example.com",
```

Create them at your DNS provider as in [install.md step 2](install.md#2-create-the-dns-records)
while the run waits (up to `rootkeel_dns_wait_seconds` for each). It asks each zone's own
nameservers directly, so a resolver that cached the names before the records existed does not
delay it. Once every nameserver answers with the VM's address, it asks certbot for the
certificate, installs certbot's renewal hook, and starts the server. If the wait runs out, create
the records and run the same command again: every step is safe to repeat, and the provider
modules find the VM that already exists.

The run ends by fetching `https://term.example.com/` on the VM itself, and then prints:

```
Rootkeel v0.1.3 answers on https://term.example.com:443/
The owner credential is in /srv/rootkeel/prod/owner on the server: sudo cat /srv/rootkeel/prod/owner
```

Check the server from your computer as in [install.md step 8](install.md#check-that-it-is-healthy),
then continue at [step 9](install.md#9-install-rt-on-your-computer). To log in to the VM, use the
address and the user the run printed: `admin` on AWS, `rootkeel` on Google Cloud and Azure,
`root` on Hetzner Cloud and DigitalOcean. (A Google Cloud project that enforces OS Login ignores
the SSH key the playbook adds, so the install cannot log in there.)

## Running it again and upgrading

Running the same command again changes nothing on a working server. The settings file is
created once from the release's `server/example.env`; later runs only rewrite the settings this
guide lists, so any other edit you make to `/etc/rootkeel/rootkeel.env` stays.

That also means a new `rootkeel_version` checks out the new release but keeps the image pins
already in the settings file. To upgrade, follow [Upgrade](install.md#upgrade) in install.md.

## Tear down

`teardown.yml` deletes the VM and all its data, including the server's owner credential and
database; on Azure it deletes the whole resource group. It has not been run against real cloud
accounts. Back up first if you may want the data again ([Back up](install.md#back-up)). Pass
the same provider settings (region, zone, location, resource group, VM name) as when you created
it, and repeat the VM name to confirm:

```sh
ansible-playbook teardown.yml -e rootkeel_cloud=aws -e rootkeel_teardown_confirm=rootkeel
ansible-playbook teardown.yml -e rootkeel_cloud=gcp -e rootkeel_gcp_zone=europe-west3-a -e rootkeel_teardown_confirm=rootkeel
ansible-playbook teardown.yml -e rootkeel_cloud=azure -e rootkeel_teardown_confirm=rootkeel
ansible-playbook teardown.yml -e rootkeel_cloud=hetzner -e rootkeel_teardown_confirm=rootkeel
ansible-playbook teardown.yml -e rootkeel_cloud=digitalocean -e rootkeel_digitalocean_region=fra1 -e rootkeel_teardown_confirm=rootkeel
```

On Azure this deletes the whole resource group (`rootkeel-rg` by default), so do not put
anything else in it. Then remove the three DNS records, and revoke the machines that used this
server ([Uninstall](install.md#uninstall)).
