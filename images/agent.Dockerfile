# SPDX-License-Identifier: AGPL-3.0-only
# A Linux machine with the agent, for tests: plain shell, pinned Zellij and an
# outbound frpc. Real machines use agent/install.sh instead; this image is never
# deployed. Every input is pinned: base image by digest, Debian packages by
# version from a dated snapshot, upstream binaries by release version and
# SHA-256, and frpc built from frp's source release (SHA-256) with a Go
# toolchain image pinned by digest, as in relay.Dockerfile.

# frpc from frp's source release with a current Go toolchain, whose standard
# library and golang.org/x fixes frp's own release binaries lack. Keep the
# pins in step with relay.Dockerfile.
FROM --platform=$BUILDPLATFORM golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e AS frpc
ARG TARGETARCH
ARG FRP_VERSION=0.71.0
ARG FRP_SOURCE_SHA256=1dd367d6d822a7fce1d3012fce0a6e778bc90c454e2c7baa0eb1e6de6054c61b
ARG FRP_GO_MODULES="golang.org/x/crypto@v0.58.0 golang.org/x/net@v0.61.0"
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN curl -fsSLo /tmp/frp.tgz "https://github.com/fatedier/frp/archive/refs/tags/v${FRP_VERSION}.tar.gz" \
 && echo "$FRP_SOURCE_SHA256  /tmp/frp.tgz" | sha256sum -c - \
 && tar -xzf /tmp/frp.tgz --strip-components=1 \
 && read -ra modules <<<"$FRP_GO_MODULES" && go get "${modules[@]}" \
 && GOOS=linux GOARCH=$TARGETARCH go build -trimpath -ldflags '-s -w' -tags frpc,noweb -o /out/frpc ./cmd/frpc \
 && cp LICENSE /out/LICENSE

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

ARG TARGETARCH
ARG DEBIAN_SNAPSHOT=20261014T083326Z
ARG ZELLIJ_VERSION=0.45.1
ARG ZELLIJ_SHA256_amd64=40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc
ARG ZELLIJ_SHA256_arm64=05f0802afadd53f8db9514e7cae53c9ae8432fed1b35b8294aa816ee3044a16b
ARG PIP_VERSION=26.2.1
ARG PIP_SHA256=71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

RUN printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\nCheck-Valid-Until: no\n' \
      "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list.d/debian.sources \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get install -y --no-install-recommends \
      ca-certificates=20250419 \
      curl=8.14.1-2+deb13u7 \
      procps=2:4.0.4-9 \
      psmisc=23.7-2 \
      less=668-1 \
      python3=3.13.5-1 \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's|^URIs: http://snapshot[.]debian[.]org/|URIs: https://snapshot.debian.org/|' /etc/apt/sources.list.d/debian.sources \
 && [ "$(grep -c '^URIs: https://snapshot.debian.org/' /etc/apt/sources.list.d/debian.sources)" = 2 ]
# From here on apt fetches over HTTPS (ca-certificates is installed now). The
# first update above has no CA certificates yet and uses HTTP; apt checks every
# index against Debian's archive keys either way.

# aiohttp and its dependencies from requirements/agent.txt, pinned by
# version and SHA-256, as wheels only: Debian trixie's python3-aiohttp lacks
# fixes the agent needs. pip (its release wheel, by SHA-256) runs from /tmp
# and is not kept.
COPY requirements/agent.txt /tmp/requirements.txt
RUN curl -fsSLo "/tmp/pip-${PIP_VERSION}-py3-none-any.whl" "https://files.pythonhosted.org/packages/py3/p/pip/pip-${PIP_VERSION}-py3-none-any.whl" \
 && echo "$PIP_SHA256  /tmp/pip-${PIP_VERSION}-py3-none-any.whl" | sha256sum -c - \
 && python3 "/tmp/pip-${PIP_VERSION}-py3-none-any.whl/pip" install --no-cache-dir --disable-pip-version-check --root-user-action ignore \
      --no-deps --require-hashes --only-binary :all: --target /usr/local/lib/python3.13/dist-packages -r /tmp/requirements.txt \
 && rm /tmp/pip-*.whl /tmp/requirements.txt \
 && python3 -c 'import aiohttp'

RUN case "$TARGETARCH" in \
      amd64) zarch=x86_64; zsum=$ZELLIJ_SHA256_amd64 ;; \
      arm64) zarch=aarch64; zsum=$ZELLIJ_SHA256_arm64 ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && cd /tmp \
 && curl -fsSLo zellij.tgz "https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-${zarch}-unknown-linux-musl.tar.gz" \
 && echo "$zsum  zellij.tgz" | sha256sum -c - \
 && tar -xzf zellij.tgz -C /usr/local/bin zellij \
 && chmod 0755 /usr/local/bin/zellij \
 && rm -f /tmp/*.tgz
COPY --from=frpc /out/frpc /usr/local/bin/frpc
COPY --from=frpc /out/LICENSE /usr/share/doc/frp/LICENSE

# The machine owner, an ordinary non-root account, and the agent's own account; the image runs as
# 1001:1001, so rt-agent's group must be gid 1001 for the helper socket's directory.
RUN useradd --create-home --shell /bin/bash --uid 1000 owner \
 && groupadd --system --gid 1001 rt-agent \
 && useradd --system --uid 1001 --gid rt-agent --home-dir /var/lib/rootkeel --shell /usr/sbin/nologin rt-agent \
 && install -d -m700 -o rt-agent -g rt-agent /var/lib/rootkeel \
 && install -d -m2710 -o owner -g rt-agent /run/rootkeel-helper

ENV LANG=C.UTF-8 TERM=xterm-256color SHELL=/bin/bash HOME=/var/lib/rootkeel PYTHONPATH=/opt/rootkeel
COPY notices/Zellij-MIT.txt /usr/share/doc/zellij/LICENSE.md
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/rootkeel/
USER 1001:1001
WORKDIR /opt/rootkeel
COPY rootkeel ./rootkeel
COPY agent ./agent
ENTRYPOINT ["python3", "-m", "rootkeel.agent"]
