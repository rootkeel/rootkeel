# SPDX-License-Identifier: AGPL-3.0-only
# frps relay for one environment. Pinned base image digest; frps is built from
# the pinned frp source release (SHA-256) with a pinned Go toolchain image, so
# it carries current Go standard-library and golang.org/x fixes that frp's own
# release binaries, built with an older Go, lack.
# The environment's frps.toml is mounted at /etc/rootkeel/frps.toml.
FROM --platform=$BUILDPLATFORM golang:1.27.2-trixie@sha256:47667934874913a449b96faa7ef4777fa285d9ec29126ee80ed61c45b7cccf0e AS build

ARG TARGETARCH
ARG FRP_VERSION=0.71.0
ARG FRP_SOURCE_SHA256=1dd367d6d822a7fce1d3012fce0a6e778bc90c454e2c7baa0eb1e6de6054c61b
# Go modules raised over frp's own go.mod for their security fixes; `go get`
# checks each against the Go checksum database. Keep in step with
# agent.Dockerfile, and drop once an frp release requires these itself.
ARG FRP_GO_MODULES="golang.org/x/crypto@v0.58.0 golang.org/x/net@v0.61.0"

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN curl -fsSLo /tmp/frp.tgz "https://github.com/fatedier/frp/archive/refs/tags/v${FRP_VERSION}.tar.gz" \
 && echo "$FRP_SOURCE_SHA256  /tmp/frp.tgz" | sha256sum -c - \
 && tar -xzf /tmp/frp.tgz --strip-components=1 \
 && read -ra modules <<<"$FRP_GO_MODULES" && go get "${modules[@]}" \
 && GOOS=linux GOARCH=$TARGETARCH go build -trimpath -ldflags '-s -w' -tags frps,noweb -o /out/frps ./cmd/frps \
 && cp LICENSE /out/LICENSE

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
COPY --from=build /out/frps /usr/local/bin/frps
COPY --from=build /out/LICENSE /usr/share/doc/frp/LICENSE
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/frps", "-c", "/etc/rootkeel/frps.toml"]
