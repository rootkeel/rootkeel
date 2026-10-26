# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/rootkeel/rootkeel/security/advisories/new>). Do not open a public issue,
discussion or pull request for a vulnerability.

Include what you found, how to reproduce it, the version or commit you tested, and the impact you
expect. We acknowledge reports within five working days and keep you informed until a fix is
released. We credit reporters in the advisory unless they ask us not to.

## Supported versions

Security fixes are made on the latest release. Rootkeel is young: upgrade to the latest release to
receive fixes.

## Scope

In scope: the broker, the agent and terminal helper, `rt`, the installer, the Compose stack, the
Helm chart and the images built from this repository. Vulnerabilities in Zellij, frp, nginx or
other upstream components should be reported to those projects; tell us too if Rootkeel's use of
them makes the issue worse.

How Rootkeel is meant to be deployed, and what it does not protect against, is described in
[docs/security.md](docs/security.md).
