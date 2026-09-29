#!/usr/bin/env node
/** Same-kernel model socket bridged through private durable regular files. */
import { createServer } from 'node:net';
import { randomBytes } from 'node:crypto';
import {
  closeSync, constants, existsSync, fsyncSync, openSync, readFileSync,
  renameSync, fstatSync, unlinkSync, writeSync,
} from 'node:fs';
import { join } from 'node:path';

const MAX_FRAME = 2 * 1024 * 1024;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function publish(path, bytes) {
  if (existsSync(path)) throw new Error('bridge response/request already exists');
  const temporary = `${path}.writing`;
  const fd = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    let position = 0;
    while (position < bytes.length) position += writeSync(fd, bytes, position);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, path);
  const dir = openSync(join(path, '..'), constants.O_RDONLY);
  try { fsyncSync(dir); } finally { closeSync(dir); }
}

function readBounded(path) {
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if (fstatSync(fd).size > MAX_FRAME) throw new Error('bridge response exceeds cap');
    const bytes = readFileSync(fd);
    if (bytes.length > MAX_FRAME) throw new Error('bridge response exceeds cap');
    return JSON.parse(bytes.toString('utf8'));
  } finally {
    closeSync(fd);
  }
}

function send(socket, value) {
  const body = Buffer.from(JSON.stringify(value));
  if (body.length > MAX_FRAME) throw new Error('bridge outbound frame exceeds cap');
  const prefix = Buffer.alloc(4);
  prefix.writeUInt32BE(body.length);
  socket.end(Buffer.concat([prefix, body]));
}

export class FileExchangeBridge {
  constructor(socketPath, bridgeDir, { deadlineMs = 300_000 } = {}) {
    if (!Number.isFinite(deadlineMs) || deadlineMs <= 0) throw new Error('invalid bridge deadline');
    this.socketPath = socketPath;
    this.bridgeDir = bridgeDir;
    this.deadlineMs = deadlineMs;
    this.server = null;
    this.active = new Set();
    this.closed = false;
  }

  async listen() {
    if (existsSync(this.socketPath)) throw new Error('model socket already exists');
    this.server = createServer((socket) => {
      this.active.add(socket);
      socket.once('close', () => this.active.delete(socket));
      this.handle(socket).catch((error) => {
        if (!socket.destroyed) {
          try { send(socket, { error: error.message }); } catch { socket.destroy(); }
        }
      });
    });
    await new Promise((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen(this.socketPath, resolve);
    });
  }

  async handle(socket) {
    const deadline = Date.now() + this.deadlineMs;
    socket.setTimeout(this.deadlineMs, () => socket.destroy(new Error('model bridge timeout')));
    const request = await new Promise((resolve, reject) => {
      let pending = Buffer.alloc(0);
      const onData = (chunk) => {
        pending = Buffer.concat([pending, chunk]);
        if (pending.length > MAX_FRAME + 4) return reject(new Error('model request exceeds cap'));
        if (pending.length < 4) return;
        const length = pending.readUInt32BE(0);
        if (!length || length > MAX_FRAME) return reject(new Error('invalid model frame length'));
        if (pending.length === length + 4) {
          socket.off('data', onData);
          resolve(pending.subarray(4));
        }
      };
      socket.on('data', onData);
      socket.once('error', reject);
      socket.once('end', () => reject(new Error('model request closed early')));
    });
    JSON.parse(request.toString('utf8'));
    const nonce = randomBytes(16).toString('hex');
    const answer = join(this.bridgeDir, `response-${nonce}.json`);
    const error = join(this.bridgeDir, `error-${nonce}.json`);
    publish(join(this.bridgeDir, `request-${nonce}.json`), request);
    while (!this.closed && Date.now() < deadline) {
      if (existsSync(answer)) return send(socket, readBounded(answer));
      if (existsSync(error)) return send(socket, readBounded(error));
      await sleep(20);
    }
    throw new Error('model bridge deadline or closure');
  }

  async close() {
    this.closed = true;
    for (const socket of this.active) socket.destroy();
    if (this.server) await new Promise((resolve) => this.server.close(resolve));
    if (existsSync(this.socketPath)) unlinkSync(this.socketPath);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [socket, bridge, timeout] = process.argv.slice(2);
  if (!socket || !bridge || !timeout) throw new Error('usage: file_exchange_bridge.mjs SOCKET BRIDGE TIMEOUT_MS');
  const server = new FileExchangeBridge(socket, bridge, { deadlineMs: Number(timeout) });
  await server.listen();
  process.on('SIGTERM', () => server.close().then(() => process.exit(0)));
  process.on('SIGINT', () => server.close().then(() => process.exit(0)));
}
