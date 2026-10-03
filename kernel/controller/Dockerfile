# Source-checkout developer controller. Runtime image ID is recorded at launch.
FROM rust:1.94-slim-bookworm@sha256:cf9dd0ec73e75f827fe59123fff9dc65af1a1c8363c3c31ee8d7f8ad0b6a5fb2 AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY kernel /src/kernel
RUN cargo build --locked --release --manifest-path /src/kernel/Cargo.toml --bin castor --bin castord

FROM docker:cli@sha256:018edbc908e08fcc9dbf029c812c34251e9b4719e6f71ca0e5eae2a987d014ca AS dockercli
FROM node:22.19.0-bookworm-slim@sha256:4a4884e8a44826194dff92ba316264f392056cbe243dcc9fd3551e71cea02b90
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=build /src/kernel/target/release/castor /usr/local/bin/castor
COPY --from=build /src/kernel/target/release/castord /usr/local/bin/castord
COPY kernel/carrier/pi/host/file_exchange_bridge.mjs /opt/castor/file_exchange_bridge.mjs
RUN mkdir -p /state /project /run/castor && chmod 0555 /project /run/castor
ENTRYPOINT ["/usr/local/bin/castor", "__controller"]
