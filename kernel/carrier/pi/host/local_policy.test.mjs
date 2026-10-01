import test from 'node:test';
import assert from 'node:assert/strict';
import { transformOllamaResponse, readBoundedJson, OllamaModelAdapter, canonicalJson, computeSha256 } from './ollama_model_adapter.mjs';

const valid = () => ({
  model: 'qwen3.5:9b', done: true, done_reason: 'stop',
  message: { role: 'assistant', content: 'ready' },
  prompt_eval_count: 7, eval_count: 5,
});

const envelope = (id = 'i-policy', maxTokens = 512) => {
  const request = { schema_version: 1, interaction_id: id, messages: [{role: 'user', content: 'repair'}], tools: [], parameters: {max_tokens: maxTokens, think: true, num_ctx: 1} };
  return { interaction_id: id, request, request_digest: computeSha256(canonicalJson(request)) };
};

test('developer POST has an explicit non-thinking profile and still honors a smaller output cap', async () => {
  let payload;
  const adapter = new OllamaModelAdapter({strictPolicy: true, fetchFn: async (_url, options) => {
    payload = JSON.parse(options.body);
    return {ok: true, json: async () => valid()};
  }});
  await adapter.handleEnvelope(envelope('i-profile', 64));
  assert.equal(payload.think, false);
  assert.deepEqual(payload.options, {temperature: 0.2, seed: 17, num_ctx: 32768, num_predict: 64});
});

test('truncation never dispatches even syntactically valid partial tool calls', () => {
  const reply = {...valid(), done_reason: 'length', eval_count: 512,
    message: {content: '', thinking: 'private reasoning', tool_calls: [{function: {name: 'castor_edit_file', arguments: {path: 'x', edits: []}}}]} };
  const outer = transformOllamaResponse(reply, 'i-truncated', {strict: true});
  const inner = JSON.parse(Buffer.from(outer.content));
  assert.equal(inner.stopReason, 'length');
  assert.equal(inner.content.some(block => block.type === 'toolCall'), false);
  assert.equal(inner.usage.output, 512);
  assert.equal(inner.provider_diagnostics.thinking_bytes, Buffer.byteLength('private reasoning'));
  assert.equal(inner.provider_diagnostics.tool_call_count, 1);
  assert.equal(inner.provider_diagnostics.failure_code, 'MODEL_OUTPUT_LIMIT_EXCEEDED');
  assert.equal(Buffer.from(outer.content).toString().includes('private reasoning'), false);
  assert.equal(outer.observation_digest, computeSha256(Buffer.from(outer.content)));
});

test('empty truncated response retains usage and replays without another provider call', async () => {
  let calls = 0;
  const adapter = new OllamaModelAdapter({strictPolicy: true, fetchFn: async () => {
    calls++;
    return {ok: true, json: async () => ({...valid(), done_reason: 'length', eval_count: 512, message: {content: '', thinking: 'hidden'}})};
  }});
  const first = await adapter.handleEnvelope(envelope());
  const second = await adapter.handleEnvelope(envelope());
  assert.deepEqual(second, first);
  assert.equal(calls, 1);
  const inner = JSON.parse(Buffer.from(first.content));
  assert.equal(inner.stopReason, 'length');
  assert.equal(inner.provider_diagnostics.content_bytes, 0);
  assert.equal(inner.usage.output, 512);
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
