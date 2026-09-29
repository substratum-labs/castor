import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createConnection } from 'node:net';
import { once } from 'node:events';
import { FileExchangeBridge } from './file_exchange_bridge.mjs';

const frame = (value) => {
  const data = Buffer.from(JSON.stringify(value));
  const prefix = Buffer.alloc(4);
  prefix.writeUInt32BE(data.length);
  return Buffer.concat([prefix, data]);
};

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const result = predicate();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('private exchange deadline');
}

test('same-kernel socket relays one bounded request through private files', async () => {
  const root = mkdtempSync(join(tmpdir(), 'castor-file-bridge-'));
  const socket = join(root, 'model.sock');
  const bridge = new FileExchangeBridge(socket, root, { deadlineMs: 2_000 });
  try {
    await bridge.listen();
    const client = createConnection(socket);
    await once(client, 'connect');
    const request = { interaction_id: 'interaction-1', request: { schema_version: 1 } };
    client.write(frame(request));
    const name = await waitFor(() => readdirSync(root).find((entry) => /^request-[0-9a-f]{32}\.json$/.test(entry)));
    const nonce = name.slice('request-'.length, -'.json'.length);
    const response = { interaction_id: 'interaction-1', content: [123], observation_digest: 'sha256:example' };
    writeFileSync(join(root, `response-${nonce}.json`), JSON.stringify(response));
    const chunks = [];
    for await (const chunk of client) chunks.push(chunk);
    const received = Buffer.concat(chunks);
    assert.equal(received.readUInt32BE(0), received.length - 4);
    assert.deepEqual(JSON.parse(received.subarray(4).toString()), response);
  } finally {
    await bridge.close();
    rmSync(root, { recursive: true, force: true });
  }
});
