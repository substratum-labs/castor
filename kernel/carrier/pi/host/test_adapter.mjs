#!/usr/bin/env node
/**
 * Test suite for OllamaModelAdapter.
 *
 * Asserts:
 * 1. Wire framing (4-byte BE length prefix, frame boundary handling, oversized frame rejection).
 * 2. Canonical request/digest checks (valid digest accepted, mutated digest rejected).
 * 3. Idempotent same-ID retry (cached result returned without duplicate fetch).
 * 4. 3-interaction cap (1st, 2nd, 3rd unique interaction IDs pass; 4th is refused).
 * 5. Token cap (num_predict <= 512 enforced on Ollama payload regardless of input).
 * 6. Model & host pin (forced to qwen3.5:9b and http://127.0.0.1:11434).
 * 7. Response conversion (transforms Ollama chat/tool responses into Castor's observation response).
 *
 * Uses mocked fetch only. Zero live network or inference calls.
 */

import assert from "node:assert/strict";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  OllamaModelAdapter,
  canonicalJson,
  computeSha256,
  formatMessagesForOllama,
  MAX_FRAME_BYTES,
  MAX_INTERACTIONS,
  MAX_TOKENS_LIMIT,
  REQUIRED_MODEL,
  OLLAMA_HOST,
  OLLAMA_PATH,
  OLLAMA_ENDPOINT,
} from "./ollama_model_adapter.mjs";

function makeTestRequest(interactionId, opts = {}) {
  const req = {
    schema_version: 1,
    interaction_id: interactionId,
    model: opts.model || "castor-task",
    messages: opts.messages || [
      {
        role: "user",
        content: "Fix the duration defect in duration.py.",
      },
    ],
    tools: opts.tools || [
      {
        name: "castor_edit_file",
        description: "Submit exact text replacements for file edit",
        parameters: {
          type: "object",
          properties: {
            path: { type: "string" },
            edits: { type: "array" },
          },
          required: ["path", "edits"],
        },
      },
    ],
    parameters: opts.parameters || { max_tokens: 512 },
  };
  return req;
}

function makeEnvelope(interactionId, opts = {}) {
  const request = makeTestRequest(interactionId, opts);
  const canon = canonicalJson(request);
  const digest = computeSha256(canon);
  return {
    interaction_id: interactionId,
    request_digest: opts.corruptDigest ? "sha256:0000000000000000000000000000000000000000000000000000000000000000" : digest,
    request,
  };
}

function sendFramed(socket, payloadObj) {
  const payloadBuf = Buffer.from(JSON.stringify(payloadObj), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32BE(payloadBuf.length, 0);
  socket.write(Buffer.concat([header, payloadBuf]));
}

function readFramed(socket) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length >= 4) {
        const len = buffer.readUInt32BE(0);
        if (buffer.length >= 4 + len) {
          socket.off("data", onData);
          socket.off("error", onError);
          const payload = buffer.subarray(4, 4 + len);
          try {
            resolve(JSON.parse(payload.toString("utf8")));
          } catch (err) {
            reject(err);
          }
        }
      }
    };
    const onError = (err) => {
      socket.off("data", onData);
      socket.off("error", onError);
      reject(err);
    };
    socket.on("data", onData);
    socket.on("error", onError);
  });
}

// ============================================================================
// Test 1: Canonical JSON and Digest Checks
// ============================================================================
async function testCanonicalDigestChecks() {
  console.log("Running: testCanonicalDigestChecks...");

  let fetchCalls = 0;
  const mockFetch = async (url, opts) => {
    fetchCalls++;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: { role: "assistant", content: "OK" },
        done_reason: "stop",
        prompt_eval_count: 10,
        eval_count: 5,
      }),
    };
  };

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });

  // 1a. Valid envelope passes
  const validEnvelope = makeEnvelope("interaction-1-1");
  const res = await adapter.handleEnvelope(validEnvelope);
  assert.equal(res.interaction_id, "interaction-1-1");
  assert.equal(fetchCalls, 1);

  // 1b. Tampered digest is rejected
  const tamperedEnvelope = makeEnvelope("interaction-1-2", { corruptDigest: true });
  await assert.rejects(
    async () => adapter.handleEnvelope(tamperedEnvelope),
    /Request digest mismatch/
  );

  // 1c. Mismatched interaction_id is rejected
  const mismatchedEnvelope = makeEnvelope("interaction-1-3");
  mismatchedEnvelope.interaction_id = "interaction-mismatched";
  await assert.rejects(
    async () => adapter.handleEnvelope(mismatchedEnvelope),
    /Interaction ID mismatch/
  );

  // 1d. Schema version mismatch is rejected
  const badSchema = makeEnvelope("interaction-1-4");
  badSchema.request.schema_version = 2;
  badSchema.request_digest = computeSha256(canonicalJson(badSchema.request));
  await assert.rejects(
    async () => adapter.handleEnvelope(badSchema),
    /Invalid request schema_version/
  );

  // 1e. Empty messages array is rejected
  const emptyMsgs = makeEnvelope("interaction-1-5");
  emptyMsgs.request.messages = [];
  emptyMsgs.request_digest = computeSha256(canonicalJson(emptyMsgs.request));
  await assert.rejects(
    async () => adapter.handleEnvelope(emptyMsgs),
    /messages must be a non-empty array/
  );

  // 1f. Finding (a): Same interaction ID with different request body/digest rejected on retry (cached)
  const reuseEnv1 = makeEnvelope("interaction-digest-reuse");
  await adapter.handleEnvelope(reuseEnv1);
  const reuseEnv2 = makeEnvelope("interaction-digest-reuse", {
    messages: [{ role: "user", content: "Completely different content" }],
  });
  await assert.rejects(
    async () => adapter.handleEnvelope(reuseEnv2),
    /Interaction ID reuse with mismatched request digest/
  );

  // 1g. Finding (a): Same interaction ID with different request body/digest rejected while in-flight
  let resolveInFlightFetch;
  const inFlightFetch = () =>
    new Promise((resolve) => {
      resolveInFlightFetch = () =>
        resolve({
          ok: true,
          status: 200,
          json: async () => ({
            model: "qwen3.5:9b",
            message: { role: "assistant", content: "done" },
            done_reason: "stop",
          }),
        });
    });
  const inFlightAdapter = new OllamaModelAdapter({ fetchFn: inFlightFetch });
  const inFlightEnv1 = makeEnvelope("interaction-inflight-mismatch");
  const p1 = inFlightAdapter.handleEnvelope(inFlightEnv1);
  const inFlightEnv2 = makeEnvelope("interaction-inflight-mismatch", {
    messages: [{ role: "user", content: "Modified content while in flight" }],
  });
  await assert.rejects(
    async () => inFlightAdapter.handleEnvelope(inFlightEnv2),
    /Interaction ID reuse with mismatched request digest/
  );
  resolveInFlightFetch();
  const res1 = await p1;
  assert.equal(res1.interaction_id, "interaction-inflight-mismatch");

  console.log("  PASS: testCanonicalDigestChecks");
}

// ============================================================================
// Test 2: Idempotent Same-ID Retries
// ============================================================================
async function testIdempotentSameIdRetry() {
  console.log("Running: testIdempotentSameIdRetry...");

  let fetchCalls = 0;
  const mockFetch = async () => {
    fetchCalls++;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: {
          role: "assistant",
          tool_calls: [
            {
              function: {
                name: "castor_edit_file",
                arguments: { path: "duration.py", edits: [{ oldText: "wrong", newText: "right" }] },
              },
            },
          ],
        },
        done_reason: "stop",
        prompt_eval_count: 50,
        eval_count: 25,
      }),
    };
  };

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });
  const env = makeEnvelope("interaction-retry-1");

  // Attempt 1: Fetch performed
  const res1 = await adapter.handleEnvelope(env);
  assert.equal(fetchCalls, 1);
  assert.equal(res1.interaction_id, "interaction-retry-1");

  // Attempt 2: Same ID retry returns cached response, zero extra fetch calls
  const res2 = await adapter.handleEnvelope(env);
  assert.equal(fetchCalls, 1, "Fetch must not be called again on retry");
  assert.deepEqual(res1, res2);

  // Attempt 3: Same ID retry again
  const res3 = await adapter.handleEnvelope(env);
  assert.equal(fetchCalls, 1, "Fetch must still be called only once");
  assert.deepEqual(res1, res3);

  // 2b. Finding (b): Concurrent same-ID requests return the same in-flight result with single fetch call
  let delayedFetchCalls = 0;
  const delayedFetch = async () => {
    delayedFetchCalls++;
    await new Promise((r) => setTimeout(r, 20));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: {
          role: "assistant",
          content: "Concurrent same-ID resolved",
        },
        done_reason: "stop",
      }),
    };
  };

  const concurrentAdapter = new OllamaModelAdapter({ fetchFn: delayedFetch });
  const concurrentEnv = makeEnvelope("int-concurrent-same");

  // Launch 3 simultaneous requests with the same interaction ID
  const [cRes1, cRes2, cRes3] = await Promise.all([
    concurrentAdapter.handleEnvelope(concurrentEnv),
    concurrentAdapter.handleEnvelope(concurrentEnv),
    concurrentAdapter.handleEnvelope(concurrentEnv),
  ]);

  assert.equal(delayedFetchCalls, 1, "Concurrent same-ID requests must trigger only 1 fetch call");
  assert.equal(cRes1.interaction_id, "int-concurrent-same");
  assert.deepEqual(cRes1, cRes2);
  assert.deepEqual(cRes1, cRes3);

  // Subsequent retry after completion also uses cache without extra fetch
  const cRes4 = await concurrentAdapter.handleEnvelope(concurrentEnv);
  assert.equal(delayedFetchCalls, 1, "Subsequent retry must not trigger additional fetch call");
  assert.deepEqual(cRes1, cRes4);

  console.log("  PASS: testIdempotentSameIdRetry");
}

// ============================================================================
// Test 3: Three-Interaction Cap
// ============================================================================
async function testThreeInteractionCap() {
  console.log("Running: testThreeInteractionCap...");

  let fetchCalls = 0;
  const mockFetch = async () => {
    fetchCalls++;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: { role: "assistant", content: `Response ${fetchCalls}` },
        done_reason: "stop",
      }),
    };
  };

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });

  // Interaction 1: OK
  const env1 = makeEnvelope("interaction-cap-1");
  const r1 = await adapter.handleEnvelope(env1);
  assert.equal(r1.interaction_id, "interaction-cap-1");
  assert.equal(fetchCalls, 1);

  // Interaction 2: OK
  const env2 = makeEnvelope("interaction-cap-2");
  const r2 = await adapter.handleEnvelope(env2);
  assert.equal(r2.interaction_id, "interaction-cap-2");
  assert.equal(fetchCalls, 2);

  // Interaction 3: OK (reached cap of 3)
  const env3 = makeEnvelope("interaction-cap-3");
  const r3 = await adapter.handleEnvelope(env3);
  assert.equal(r3.interaction_id, "interaction-cap-3");
  assert.equal(fetchCalls, 3);

  // Interaction 4: Refused! Must throw interaction cap reached
  const env4 = makeEnvelope("interaction-cap-4");
  await assert.rejects(
    async () => adapter.handleEnvelope(env4),
    /Interaction cap reached/
  );
  assert.equal(fetchCalls, 3, "4th unique interaction must not trigger fetch");

  // But retries on existing interaction 1, 2, or 3 still work via cache:
  const retry1 = await adapter.handleEnvelope(env1);
  assert.equal(retry1.interaction_id, "interaction-cap-1");
  assert.equal(fetchCalls, 3);

  // 3b. Finding (b): Concurrent unique requests count in-flight toward cap; 4th concurrent unique ID is rejected
  let concurrentUniqueFetches = 0;
  const delayedUniqueFetch = async () => {
    concurrentUniqueFetches++;
    await new Promise((r) => setTimeout(r, 20));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: { role: "assistant", content: `Unique response ${concurrentUniqueFetches}` },
        done_reason: "stop",
      }),
    };
  };

  const capAdapter = new OllamaModelAdapter({ fetchFn: delayedUniqueFetch });
  const envA = makeEnvelope("int-simult-1");
  const envB = makeEnvelope("int-simult-2");
  const envC = makeEnvelope("int-simult-3");
  const envD = makeEnvelope("int-simult-4");

  // Fire 4 simultaneous unique requests
  const settled = await Promise.allSettled([
    capAdapter.handleEnvelope(envA),
    capAdapter.handleEnvelope(envB),
    capAdapter.handleEnvelope(envC),
    capAdapter.handleEnvelope(envD),
  ]);

  const fulfilled = settled.filter((s) => s.status === "fulfilled");
  const rejected = settled.filter((s) => s.status === "rejected");

  assert.equal(fulfilled.length, 3, "Exactly 3 unique concurrent requests must succeed");
  assert.equal(rejected.length, 1, "The 4th concurrent unique request must be rejected");
  assert.match(rejected[0].reason.message, /Interaction cap reached/);
  assert.equal(concurrentUniqueFetches, 3, "Only 3 fetches must be initiated");

  console.log("  PASS: testThreeInteractionCap");
}

// ============================================================================
// Test 4: Token Cap and Model Pin Enforcement
// ============================================================================
async function testTokenCapAndModelPin() {
  console.log("Running: testTokenCapAndModelPin...");

  let capturedPayload = null;
  let capturedUrl = null;

  const mockFetch = async (url, opts) => {
    capturedUrl = url;
    capturedPayload = JSON.parse(opts.body);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: { role: "assistant", content: "Capped" },
        done_reason: "stop",
      }),
    };
  };

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });

  // 4a. Client requests 2048 tokens -> Adapter strictly caps num_predict to 512
  const envOversized = makeEnvelope("int-tokens-1", {
    model: "untrusted-model",
    parameters: { max_tokens: 2048 },
  });
  await adapter.handleEnvelope(envOversized);

  assert.equal(capturedUrl, "http://127.0.0.1:11434/api/chat");
  assert.equal(capturedPayload.model, "qwen3.5:9b", "Model must be pinned to qwen3.5:9b");
  assert.equal(capturedPayload.options.num_predict, 512, "num_predict must be capped <= 512");

  // 4b. Client requests 128 tokens -> Adapter respects lower limit
  const envLower = makeEnvelope("int-tokens-2", {
    parameters: { max_tokens: 128 },
  });
  await adapter.handleEnvelope(envLower);
  assert.equal(capturedPayload.options.num_predict, 128);

  // 4c. Client requests no parameters -> Adapter defaults to 512
  const envDefault = makeEnvelope("int-tokens-3", {
    parameters: {},
  });
  await adapter.handleEnvelope(envDefault);
  assert.equal(capturedPayload.options.num_predict, 512);

  // 4d. Finding (c): Validate requested max_tokens as a finite positive integer before capping to 512
  for (const badTokens of [0, -1, -500, 3.14, "512", true, false, [], {}]) {
    const badTokenEnv = makeEnvelope(`int-bad-token-${String(badTokens)}`, {
      parameters: { max_tokens: badTokens },
    });
    await assert.rejects(
      async () => adapter.handleEnvelope(badTokenEnv),
      /Invalid max_tokens: expected finite positive integer/
    );
  }
  for (const nonFinite of [NaN, Infinity, -Infinity]) {
    const badReq = makeTestRequest(`int-bad-nonfinite-${String(nonFinite)}`);
    badReq.parameters = { max_tokens: nonFinite };
    const badEnv = {
      interaction_id: badReq.interaction_id,
      request_digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      request: badReq,
    };
    await assert.rejects(
      async () => adapter.handleEnvelope(badEnv),
      /Invalid max_tokens: expected finite positive integer/
    );
  }

  // 4e. Finding (d): Endpoint exact protocol, host, and path pinning
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "https://127.0.0.1:11434/api/chat" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "http://localhost:11434/api/chat" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "http://192.168.1.5:11434/api/chat" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "http://127.0.0.1:11434/api/generate" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "http://127.0.0.1:11434/api/chat?bypass=1" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );
  assert.throws(
    () => new OllamaModelAdapter({ ollamaEndpoint: "http://127.0.0.1:11434/api/chat#fragment" }),
    /Security violation: endpoint must strictly connect to http:\/\/127.0.0.1:11434\/api\/chat/
  );

  // 4f. Finding (d): Verify redirect: "error" is passed to fetchFn and redirects fail
  let capturedRedirect = null;
  const redirectCheckFetch = async (url, opts) => {
    capturedRedirect = opts.redirect;
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        message: { role: "assistant", content: "redirect-check" },
        done_reason: "stop",
      }),
    };
  };
  const redirectAdapter = new OllamaModelAdapter({ fetchFn: redirectCheckFetch });
  await redirectAdapter.handleEnvelope(makeEnvelope("int-redirect-check"));
  assert.equal(capturedRedirect, "error", "fetch options must strictly set redirect: 'error'");

  // Also verify that a fetch redirect error rejection propagates correctly
  const redirectErrorFetch = async (url, opts) => {
    if (opts.redirect === "error") {
      throw new TypeError("fetch failed: unexpected redirect");
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const redirectErrorAdapter = new OllamaModelAdapter({ fetchFn: redirectErrorFetch });
  await assert.rejects(
    async () => redirectErrorAdapter.handleEnvelope(makeEnvelope("int-redirect-error")),
    /unexpected redirect/
  );

  console.log("  PASS: testTokenCapAndModelPin");
}

// ============================================================================
// Test 5: Response Conversion (Ollama to Castor Schema)
// ============================================================================
async function testResponseConversion() {
  console.log("Running: testResponseConversion...");

  // 5a: Tool Call Conversion
  const mockToolOllama = {
    model: "qwen3.5:9b",
    created_at: "2026-09-25T18:00:00Z",
    message: {
      role: "assistant",
      content: "Let me fix the duration calculation.",
      tool_calls: [
        {
          id: "call_abc123",
          function: {
            name: "castor_edit_file",
            arguments: {
              path: "duration.py",
              edits: [{ oldText: "return value * 3600", newText: "return value * 86400" }],
            },
          },
        },
      ],
    },
    done_reason: "stop",
    prompt_eval_count: 84,
    eval_count: 42,
  };

  const adapterTool = new OllamaModelAdapter({
    fetchFn: async () => ({
      ok: true,
      status: 200,
      json: async () => mockToolOllama,
    }),
  });

  const toolResp = await adapterTool.handleEnvelope(makeEnvelope("int-conv-tool"));

  // Check outer Castor wire structure matching model.rs expectations
  assert.equal(toolResp.interaction_id, "int-conv-tool");
  assert.equal(toolResp.observation_region_id, "region://model-observation/int-conv-tool");
  assert.ok(toolResp.observation_digest.startsWith("sha256:"));
  assert.ok(Array.isArray(toolResp.content));

  // Verify content bytes match digest
  const contentBuf = Buffer.from(toolResp.content);
  const actualDigest = computeSha256(contentBuf);
  assert.equal(actualDigest, toolResp.observation_digest);

  // Verify inner JSON parsed by Pi / Castor
  const innerTool = JSON.parse(contentBuf.toString("utf8"));
  assert.equal(innerTool.stopReason, "toolUse");
  assert.equal(innerTool.usage.input, 84);
  assert.equal(innerTool.usage.output, 42);

  // Content blocks contain both text and toolCall
  assert.equal(innerTool.content.length, 2);
  assert.equal(innerTool.content[0].type, "text");
  assert.equal(innerTool.content[0].text, "Let me fix the duration calculation.");
  assert.equal(innerTool.content[1].type, "toolCall");
  assert.equal(innerTool.content[1].name, "castor_edit_file");
  assert.equal(innerTool.content[1].arguments.path, "duration.py");
  assert.ok(innerTool.content[1].arguments.edits[0].newText.includes("86400"));

  // 5b: Plain Text Completion Conversion
  const mockTextOllama = {
    model: "qwen3.5:9b",
    message: {
      role: "assistant",
      content: "All tests now pass. Task complete.",
    },
    done_reason: "stop",
    prompt_eval_count: 120,
    eval_count: 8,
  };

  const adapterText = new OllamaModelAdapter({
    fetchFn: async () => ({
      ok: true,
      status: 200,
      json: async () => mockTextOllama,
    }),
  });

  const textResp = await adapterText.handleEnvelope(makeEnvelope("int-conv-text"));
  const textContentBuf = Buffer.from(textResp.content);
  assert.equal(computeSha256(textContentBuf), textResp.observation_digest);

  const innerText = JSON.parse(textContentBuf.toString("utf8"));
  assert.equal(innerText.stopReason, "stop");
  assert.equal(innerText.content.length, 1);
  assert.equal(innerText.content[0].type, "text");
  assert.equal(innerText.content[0].text, "All tests now pass. Task complete.");
  assert.equal(innerText.usage.input, 120);
  assert.equal(innerText.usage.output, 8);

  console.log("  PASS: testResponseConversion");
}

// ============================================================================
// Test 6: UDS Socket Framing (Wire Level)
// ============================================================================
async function testUdsSocketFraming() {
  console.log("Running: testUdsSocketFraming...");

  const socketPath = join(tmpdir(), `castor_model_test_${Date.now()}_${Math.random().toString(36).slice(2)}.sock`);

  const mockFetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      model: "qwen3.5:9b",
      message: { role: "assistant", content: "Wire frame test ok" },
      done_reason: "stop",
      prompt_eval_count: 15,
      eval_count: 6,
    }),
  });

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });
  await adapter.listen(socketPath);

  try {
    const client = createConnection({ path: socketPath });
    await new Promise((res) => client.once("connect", res));

    // Send framed request
    const env = makeEnvelope("int-wire-1");
    sendFramed(client, env);

    // Read framed response
    const resp = await readFramed(client);

    assert.equal(resp.interaction_id, "int-wire-1");
    assert.equal(resp.observation_region_id, "region://model-observation/int-wire-1");
    assert.ok(resp.observation_digest.startsWith("sha256:"));

    const contentBytes = Buffer.from(resp.content);
    assert.equal(computeSha256(contentBytes), resp.observation_digest);

    const parsedInner = JSON.parse(contentBytes.toString("utf8"));
    assert.equal(parsedInner.content[0].text, "Wire frame test ok");
    assert.equal(parsedInner.stopReason, "stop");

    for (const id of ["int-wire-2", "int-wire-3"]) {
      sendFramed(client, makeEnvelope(id));
      assert.equal((await readFramed(client)).interaction_id, id);
    }
    sendFramed(client, makeEnvelope("int-wire-4"));
    const rejected = await readFramed(client);
    assert.equal(rejected.error.code, "INTERACTION_BUDGET_EXHAUSTED");
    sendFramed(client, env);
    assert.equal((await readFramed(client)).interaction_id, "int-wire-1", "adapter remains alive for cached retries");

    client.destroy();
  } finally {
    await adapter.close();
  }

  console.log("  PASS: testUdsSocketFraming");
}

// ============================================================================
// Test 7: Assistant Tool Call Followed by Tool Result (Call Linkage)
// ============================================================================
async function testAssistantToolCallFollowedByToolResult() {
  console.log("Running: testAssistantToolCallFollowedByToolResult...");

  // 7a. Direct formatMessagesForOllama validation with exact Pi 0.87.1 types
  const piMessages = [
    {
      role: "system",
      content: "",
      sections: { preamble: "You are an expert coding assistant..." },
    },
    {
      role: "user",
      content: [{ type: "text", text: "Fix the duration defect in duration.py." }],
    },
    {
      role: "assistant",
      content: [
        { type: "text", text: "I will update the duration calculation in duration.py." },
        {
          type: "toolCall",
          id: "tool-edit-1",
          name: "castor_edit_file",
          arguments: {
            path: "duration.py",
            edits: [{ oldText: "return value * 3600", newText: "return value * 86400" }],
          },
        },
      ],
      api: "castor-buffered",
      provider: "castor",
      model: "castor-task",
      stopReason: "toolUse",
    },
    {
      role: "toolResult",
      toolCallId: "tool-edit-1",
      toolName: "castor_edit_file",
      content: [
        {
          type: "text",
          text: "Edit submitted to Castor for trusted host settlement and verification.",
        },
      ],
      details: { action_id: "action-1" },
      isError: false,
    },
  ];

  const formatted = formatMessagesForOllama(piMessages);

  assert.equal(formatted.length, 4);
  assert.equal(formatted[0].role, "system");
  assert.equal(formatted[0].content, "You are an expert coding assistant...");

  const updatedSystem = formatMessagesForOllama([
    { role: "system", content: [{ type: "text", text: "Base" }], sections: { rules: "First rule" } },
    { role: "system", content: "", sections: { rules: "Second rule", old: null } },
  ]);
  assert.equal(updatedSystem[0].content, "Base\n\nFirst rule");
  assert.equal(updatedSystem[1].content, 'Updated system prompt section "rules":\n\nSecond rule\n\nRemoved system prompt section "old".');

  assert.equal(formatted[1].role, "user");
  assert.equal(formatted[1].content, "Fix the duration defect in duration.py.");

  // Assistant message formatting
  const asst = formatted[2];
  assert.equal(asst.role, "assistant");
  assert.equal(asst.content, "I will update the duration calculation in duration.py.");
  assert.ok(Array.isArray(asst.tool_calls), "assistant message must contain tool_calls");
  assert.equal(asst.tool_calls.length, 1);
  assert.equal(asst.tool_calls[0].id, "tool-edit-1");
  assert.equal(asst.tool_calls[0].type, "function");
  assert.equal(asst.tool_calls[0].function.name, "castor_edit_file");
  assert.equal(asst.tool_calls[0].function.arguments.path, "duration.py");

  // Tool result message formatting & call linkage
  const toolMsg = formatted[3];
  assert.equal(toolMsg.role, "tool");
  assert.equal(toolMsg.content, "Edit submitted to Castor for trusted host settlement and verification.");
  assert.equal(toolMsg.tool_call_id, "tool-edit-1", "tool_call_id must match the originating assistant tool call ID");
  assert.equal(toolMsg.tool_name, "castor_edit_file", "tool_name must match the originating assistant tool name");

  // Direct linkage assertion
  assert.equal(toolMsg.tool_call_id, asst.tool_calls[0].id, "tool result tool_call_id must link directly to assistant tool_calls[0].id");
  assert.equal(toolMsg.tool_name, asst.tool_calls[0].function.name, "tool result tool_name must link directly to assistant tool_calls[0].function.name");

  // 7b. Also verify snake_case and string content fallbacks
  const fallbackMessages = [
    {
      role: "assistant",
      content: "",
      tool_calls: [
        {
          id: "call_def456",
          type: "function",
          function: { name: "castor_read_file", arguments: { path: "README.md" } },
        },
      ],
    },
    {
      role: "toolResult",
      tool_call_id: "call_def456",
      tool_name: "castor_read_file",
      content: "File contents here",
    },
  ];
  const formattedFallbacks = formatMessagesForOllama(fallbackMessages);
  assert.equal(formattedFallbacks[0].tool_calls[0].id, "call_def456");
  assert.equal(formattedFallbacks[1].tool_call_id, "call_def456");
  assert.equal(formattedFallbacks[1].tool_name, "castor_read_file");
  assert.equal(formattedFallbacks[1].content, "File contents here");

  // 7c. Full Adapter handleEnvelope test with mocked fetch
  let capturedOllamaPayload = null;
  const mockFetch = async (url, opts) => {
    assert.equal(url, OLLAMA_ENDPOINT);
    assert.equal(opts.method, "POST");
    assert.equal(opts.redirect, "error");
    capturedOllamaPayload = JSON.parse(opts.body);
    return {
      ok: true,
      status: 200,
      json: async () => ({
        model: "qwen3.5:9b",
        created_at: "2026-09-25T18:05:00Z",
        message: {
          role: "assistant",
          content: "The unit tests now pass. Task complete.",
        },
        done_reason: "stop",
        prompt_eval_count: 95,
        eval_count: 14,
      }),
    };
  };

  const adapter = new OllamaModelAdapter({ fetchFn: mockFetch });
  const envelope = makeEnvelope("int-turn-2", {
    messages: piMessages,
  });

  const response = await adapter.handleEnvelope(envelope);

  // Verify outgoing Ollama payload has preserved linkage
  assert.ok(capturedOllamaPayload, "mock fetch must have been called");
  assert.equal(capturedOllamaPayload.model, REQUIRED_MODEL);
  assert.ok(Array.isArray(capturedOllamaPayload.messages));
  assert.equal(capturedOllamaPayload.messages.length, 4);

  const outAsst = capturedOllamaPayload.messages[2];
  const outTool = capturedOllamaPayload.messages[3];

  assert.equal(outAsst.role, "assistant");
  assert.equal(outAsst.tool_calls[0].id, "tool-edit-1");
  assert.equal(outTool.role, "tool");
  assert.equal(outTool.tool_call_id, "tool-edit-1");
  assert.equal(outTool.tool_name, "castor_edit_file");
  assert.equal(outTool.tool_call_id, outAsst.tool_calls[0].id);
  assert.equal(outTool.tool_name, outAsst.tool_calls[0].function.name);

  // Verify adapter response transformation
  assert.equal(response.interaction_id, "int-turn-2");
  const parsedResp = JSON.parse(Buffer.from(response.content).toString("utf8"));
  assert.equal(parsedResp.stopReason, "stop");
  assert.equal(parsedResp.content[0].text, "The unit tests now pass. Task complete.");

  console.log("  PASS: testAssistantToolCallFollowedByToolResult");
}

// ============================================================================
// Main Runner
// ============================================================================
async function main() {
  console.log("=== Starting OllamaModelAdapter Mock Test Suite ===");
  await testCanonicalDigestChecks();
  await testIdempotentSameIdRetry();
  await testThreeInteractionCap();
  await testTokenCapAndModelPin();
  await testResponseConversion();
  await testUdsSocketFraming();
  await testAssistantToolCallFollowedByToolResult();
  console.log("=== ALL 7 ADAPTER TESTS PASSED (100% Mocked, 0 Network Calls) ===");
}

main().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
