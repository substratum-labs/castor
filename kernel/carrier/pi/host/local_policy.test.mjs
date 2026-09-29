import test from 'node:test';
import assert from 'node:assert/strict';
import { transformOllamaResponse, readBoundedJson } from './ollama_model_adapter.mjs';

const valid = () => ({
  model: 'qwen3.5:9b', done: true, done_reason: 'stop',
  message: { role: 'assistant', content: 'ready' },
  prompt_eval_count: 7, eval_count: 5,
});

test('developer mode rejects missing or excessive usage before native observation', () => {
  const missing = valid();
  delete missing.eval_count;
  assert.throws(() => transformOllamaResponse(missing, 'i-1', { strict: true }), /usage/);
  assert.throws(() => transformOllamaResponse({ ...valid(), eval_count: 513 }, 'i-1', { strict: true }), /usage/);
});

test('developer mode rejects wrong model and unfinished output', () => {
  assert.throws(() => transformOllamaResponse({ ...valid(), model: 'other' }, 'i-1', { strict: true }), /model/);
  assert.throws(() => transformOllamaResponse({ ...valid(), done: false }, 'i-1', { strict: true }), /unfinished/);
  const observed = transformOllamaResponse(valid(), 'i-1', { strict: true });
  assert.equal(JSON.parse(Buffer.from(observed.content).toString()).usage.output, 5);
});

test('developer mode captures one installed model digest and rejects a later retag', async () => {
  const digest = 'a'.repeat(64);
  const replies = [
    { models: [{ name: 'qwen3.5:9b', digest }] },
    { version: '0.34.1' },
  ];
  let called = 0;
  const fetchFn = async () => ({ ok: true, json: async () => replies[called++] });
  const { readLocalModelPin } = await import('./ollama_model_adapter.mjs');
  const pin = await readLocalModelPin(fetchFn);
  assert.deepEqual(pin, { name: 'qwen3.5:9b', digest, version: '0.34.1' });
  const wrong = async () => ({ ok: true, json: async () => ({ models: [{ name: 'qwen3.5:9b', digest: 'b'.repeat(64) }] }) });
  await assert.rejects(readLocalModelPin(wrong), /metadata|version/);
});

test('provider reply is bounded before JSON parsing', async () => {
  const response = { body: new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(1025));
      controller.close();
    },
  }) };
  await assert.rejects(readBoundedJson(response, 1024), /exceeds cap/);
});
