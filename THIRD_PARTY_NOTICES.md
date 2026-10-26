# Third-party notices

Rootkeel itself is licensed under the GNU Affero General Public License, version 3 only
([LICENSE](LICENSE)). It downloads, builds or runs the third-party components below, each pinned
to an exact version and verified by checksum or digest. Anyone who redistributes images or
binaries built from this repository must carry these components' licence texts and notices
(Apache-2.0 components also their NOTICE files, where present).

## Run on the server

| Component | Pin | Used for | Licence |
| --- | --- | --- | --- |
| [nginx](https://nginx.org), official Docker image | `nginx:1.30.1-alpine@sha256:c819f83c…45874d` | TLS front door | [BSD-2-Clause](https://github.com/nginx/nginx/blob/release-1.30.1/LICENSE); Alpine packages under their own licences |
| [frp](https://github.com/fatedier/frp) (frps) | 0.71.0, built from the source archive (SHA-256) with `golang.org/x/crypto` v0.58.0 and `golang.org/x/net` v0.61.0, without the web dashboard | The relay | [Apache-2.0](https://github.com/fatedier/frp/blob/v0.71.0/LICENSE), installed as `/usr/share/doc/frp/LICENSE`; its Go module dependencies are compiled in |
| [Python](https://www.python.org), official Docker image | `python:3.13.16-slim-trixie@sha256:70729b46…5a3678c2f` | The broker's interpreter | PSF-2.0 and bundled notices; Debian packages under their own licences in `/usr/share/doc` |
| [aiohttp](https://github.com/aio-libs/aiohttp) and its dependencies | Wheels pinned by version and SHA-256 in `requirements/broker.txt`: aiohttp 3.14.5, aiohappyeyeballs, aiosignal, attrs, frozenlist, idna, multidict, propcache, yarl | HTTP and WebSocket server and client | aiohttp, aiosignal, frozenlist, multidict, propcache, yarl: Apache-2.0 (aiohttp's vendored llhttp: MIT); aiohappyeyeballs: PSF-2.0; attrs: MIT; idna: BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 with cffi and pycparser, pinned in `requirements/broker.txt` | AES-GCM sealing of stored credentials | Apache-2.0 OR BSD-3-Clause; cffi: MIT-0; pycparser: BSD-3-Clause |
| [Debian](https://www.debian.org) `trixie-slim` | `debian@sha256:a29215f6…49f11f` | Base of the relay and agent test images | Per-package `/usr/share/doc/*/copyright` |
| [Go](https://go.dev), official Docker image | `golang:1.27.2-trixie@sha256:47667934…cccf0e`, build stage only | Compiling frps and frpc | [BSD-3-Clause](https://go.dev/LICENSE); the standard library compiled into the binaries is under the same licence |
| [Docker Compose](https://github.com/docker/compose), [Docker Buildx](https://github.com/docker/buildx) | 5.5.1 and 0.37.1, release binaries by SHA-256, fetched only when the host lacks them | Running and building the stack | Apache-2.0 |

## Installed on shared machines

| Component | Pin | Used for | Licence |
| --- | --- | --- | --- |
| [Zellij](https://github.com/zellij-org/zellij) | 0.45.1, release tarball by SHA-256 | Sessions, web server and web client | [MIT](https://github.com/zellij-org/zellij/blob/v0.45.1/LICENSE.md); the release tarball omits it, so `notices/Zellij-MIT.txt` carries it and the installer installs it as `/usr/share/doc/rootkeel/zellij-LICENSE.md` |
| [frp](https://github.com/fatedier/frp) (frpc) | 0.71.0, release archive by SHA-256 (the agent test image builds it from source as above) | The agent's outbound tunnel | Apache-2.0, installed as `/usr/share/doc/rootkeel/frp-LICENSE` |
| Python 3, python3-aiohttp, ca-certificates, curl | The distribution's maintained packages | The agent | Per-package `/usr/share/doc/*/copyright` |

The agent test image (`images/agent.Dockerfile`, never deployed) installs Debian 13 packages from a
dated snapshot (ca-certificates, curl, procps, psmisc, less, python3) and the aiohttp wheels pinned
in `requirements/agent.txt`, with pip's release wheel (MIT), which is not kept.

## In the standalone rt downloads

Each release's `rt-<version>-<platform>.tar.gz` is `rt` frozen with PyInstaller into one
executable. Its `licenses/` directory carries the texts below.

| Component | Pin | Used for | Licence |
| --- | --- | --- | --- |
| [Python](https://www.python.org) 3.13 runtime and standard library | Linux: `python:3.13.16-bookworm@sha256:dc02fc25…949c166df` (with the Debian 12 libraries it loads, such as OpenSSL 3 and zlib); macOS: the Python 3.13 that `actions/setup-python` installs | Running `rt` | PSF-2.0 with the notices of its bundled components, as `licenses/Python-LICENSE.txt`; OpenSSL 3: Apache-2.0; Debian packages under their own licences |
| [PyInstaller](https://pyinstaller.org) bootloader | 6.22.3, pinned with its dependencies in `requirements/release.txt` | Starting the frozen `rt` | GPL-2.0-or-later with the bootloader exception, which allows distributing the executable under Rootkeel's licence; as `licenses/PyInstaller-COPYING.txt` |

## Build tooling

| Component | Pin | Used for | Licence |
| --- | --- | --- | --- |
| [syft](https://github.com/anchore/syft) | `anchore/syft:v1.54.1@sha256:3eb5379b…4f49b1aa` | SPDX SBOMs of built images | Apache-2.0 |
| [BuildKit](https://github.com/moby/buildkit) | `moby/buildkit:v0.34.0@sha256:b059f8d7…4d9ee87c` (`build-images.sh --push` only) | Pushing images with SBOM and provenance attestations | Apache-2.0 |
| [buildkit-syft-scanner](https://github.com/docker/buildkit-syft-scanner) | `docker/buildkit-syft-scanner:1.12.0@sha256:ae4f3b55…93654d6a9` (`build-images.sh --push` only) | BuildKit's SBOM generator | Apache-2.0 |

| [PyInstaller](https://pyinstaller.org), altgraph, macholib, packaging, pyinstaller-hooks-contrib, setuptools | Pinned by version and SHA-256 in `requirements/release.txt` | Freezing the standalone `rt` (release workflow, `scripts/build-rt.sh`) | PyInstaller: GPL-2.0-or-later with the bootloader exception; pyinstaller-hooks-contrib: GPL-2.0-or-later OR Apache-2.0; altgraph, macholib, setuptools: MIT; packaging: Apache-2.0 OR BSD-2-Clause |
| [binfmt](https://github.com/tonistiigi/binfmt) (QEMU user emulation) | `tonistiigi/binfmt:qemu-v10.2.3@sha256:400a4873…b21166f0` (release workflow only) | Building and checking ARM64 images and downloads on x86-64 runners | MIT; QEMU: GPL-2.0 |
| [Distribution registry](https://github.com/distribution/distribution) | `registry:2@sha256:a3d8aaa6…32415373` (release workflow dry runs only) | A registry inside the job for dry-run image pushes | Apache-2.0 |
| Debian 13 and Ubuntu 24.04 | `debian:13@sha256:4c252754…3b308`, `ubuntu:24.04@sha256:68ee2200…aff33ac` (tests only) | Clean machines for the agent installer checks | Per-package `/usr/share/doc/*/copyright` |

None of these build tools ends up in an image. Of PyInstaller, only its bootloader ends up in the
standalone `rt`, as above.

## Files derived from third parties

`notices/Zellij-MIT.txt` is the unmodified MIT licence from Zellij v0.45.1 (SHA-256
`9aa6c363b18a48eeecbffe13ff390c76ede2c90eb37ba17dfbbaffc42892d87d`). `LICENSE` and
`LICENSES/AGPL-3.0-only.txt` are the GNU Affero General Public License, version 3;
`LICENSES/MIT.txt` is the MIT licence text. The Zellij configuration and layout under
`agent/zellij/` are written for this project; option names follow Zellij's documented
configuration.
