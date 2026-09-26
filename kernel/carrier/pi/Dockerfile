FROM node:22.19.0-bookworm-slim@sha256:4a4884e8a44826194dff92ba316264f392056cbe243dcc9fd3551e71cea02b90

WORKDIR /opt/castor
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && ln -s /opt/castor/node_modules/.bin/pi /usr/local/bin/pi \
    && mkdir -p /workspace /run/castor \
    && chmod 0555 /workspace /run/castor
COPY --chown=10001:10001 --chmod=0444 protocol.js castor-pi-extension.js ./

ENV HOME=/tmp
USER 10001:10001
