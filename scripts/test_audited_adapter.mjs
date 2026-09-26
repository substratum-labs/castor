#!/usr/bin/env node
/**
 * Test suite for AuditedOllamaModelAdapter.
 *
 * Asserts:
 * 1. Produces adapter_events.jsonl, ollama_calls.jsonl, and adapter_summary.json.
 * 2. Correctly counts and logs each unique Ollama HTTP call.
 * 3. Enforces bounded num_predict <= 512.
 * 4. Logs stop reason, token usage, and latency.
 * 5. Logs wire-framed errors for budget exhaustion and adapter errors.
 * 6. Logs cache hits on idempotent retries without duplicate HTTP calls.
 * 7. Strictly avoids logging secrets or user message contents.
 */

import assert from "node:assert/strict";
import { readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createConnection } from "node:net";
import { AuditedOllamaModelAdapter } from "./audited_adapter_wrapper.mjs";
import {
  canonicalJson,
  computeSha256,
} from "../kernel/carrier/pi/host/ollama_model_adapter.mjs";

function makeEnvelope(interactionId, opts = {}) {
  const secretContent = opts.secretContent || "Fix the duration defect in duration.py.";
  const request = {
    schema_version: 1,
    interaction_id: interactionId,
    model: "castor-task",
    messages: [
      {
        role: "user",
        content: secretContent,
      },
    ],
    tools: [
      {
        name: "castor_edit_file",
        description: "Submit unified diff for file edit",
        parameters: {
          type: "object",
          properties: {
            path: { type: "string" },
            patch_diff: { type: "string" },
          },
          required: ["path", "patch_diff"],
        },
      },
    ],
    parameters: opts.parameters || { max_tokens: 512 },
  };
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

async function runTests() {
  const testDir = join(tmpdir(), `castor-aud-${Date.now()}`);
  const socketPath = join("/tmp", `aud_${Date.now()}_${Math.random().toString(36).slice(2, 6)}.sock`);
  const evidenceDir = join(testDir, "evidence");

  console.log(`Setting up test environment in ${testDir}...`);

  let mockCallCount = 0;
  const mockFetch = async (url, options) => {
    mockCallCount += 1;
    const body = JSON.parse(options.body);
    assert.equal(body.model, "qwen3.5:9b");
    assert.ok(body.options.num_predict <= 512);

    const mockResponse = {
      model: "qwen3.5:9b",
      message: {
        role: "assistant",
        content: "Here is the fix.",
        tool_calls: [
          {
            function: {
              name: "castor_edit_file",
              arguments: {
                path: "duration.py",
                patch_diff: "--- a/duration.py\n+++ b/duration.py\n@@ -41,1 +41,1 @@\n-        return value * 3600\n+        return value * 86400\n",
              },
            },
          },
        ],
      },
      done: true,
      done_reason: "stop",
      prompt_eval_count: 120,
      eval_count: 45,
    };

    return new Response(JSON.stringify(mockResponse), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const adapter = new AuditedOllamaModelAdapter({
    evidenceDir,
    fetchFn: mockFetch,
    maxInteractions: 3,
    maxTokens: 512,
  });

  try {
    await adapter.listen(socketPath);

    // 1. Perform 1st valid interaction over UDS socket
    const client = createConnection(socketPath);
    await new Promise((resolve) => client.once("connect", resolve));

    const secretMarker = "SUPER_SECRET_TOKEN_DO_NOT_LOG";
    const env1 = makeEnvelope("interaction-1-1", { secretContent: `Secret task instruction: ${secretMarker}` });
    sendFramed(client, env1);
    const resp1 = await readFramed(client);

    assert.equal(resp1.interaction_id, "interaction-1-1");
    assert.ok(resp1.observation_digest.startsWith("sha256:"));

    // 2. Perform idempotent retry with same interaction ID (should hit cache, NOT call fetch)
    sendFramed(client, env1);
    const resp1Retry = await readFramed(client);
    assert.equal(resp1Retry.observation_digest, resp1.observation_digest);
    assert.equal(mockCallCount, 1, "Cache hit must not increment Ollama HTTP calls");

    // 3. Perform 2nd unique interaction
    const env2 = makeEnvelope("interaction-1-2");
    sendFramed(client, env2);
    const resp2 = await readFramed(client);
    assert.equal(resp2.interaction_id, "interaction-1-2");
    assert.equal(mockCallCount, 2);

    // 4. Perform 3rd unique interaction
    const env3 = makeEnvelope("interaction-1-3");
    sendFramed(client, env3);
    const resp3 = await readFramed(client);
    assert.equal(resp3.interaction_id, "interaction-1-3");
    assert.equal(mockCallCount, 3);

    // 5. Attempt 4th unique interaction (must trigger INTERACTION_BUDGET_EXHAUSTED)
    const env4 = makeEnvelope("interaction-1-4");
    sendFramed(client, env4);
    const resp4 = await readFramed(client);
    assert.ok(resp4.error, "4th interaction must return wire error");
    assert.equal(resp4.error.code, "INTERACTION_BUDGET_EXHAUSTED");
    assert.equal(mockCallCount, 3, "Refused 4th call must not dispatch HTTP request");

    // 6. Test digest corruption error
    const envCorrupt = makeEnvelope("interaction-corrupt", { corruptDigest: true });
    sendFramed(client, envCorrupt);
    const respCorrupt = await readFramed(client);
    assert.ok(respCorrupt.error, "Corrupt digest must return wire error");
    assert.equal(respCorrupt.error.code, "MODEL_ADAPTER_ERROR");

    client.end();
    await adapter.close();

    // Verify Evidence Artifacts
    console.log("Verifying produced evidence files...");
    const eventsFile = join(evidenceDir, "adapter_events.jsonl");
    const callsFile = join(evidenceDir, "ollama_calls.jsonl");
    const summaryFile = join(evidenceDir, "adapter_summary.json");

    assert.ok(existsSync(eventsFile), "adapter_events.jsonl must exist");
    assert.ok(existsSync(callsFile), "ollama_calls.jsonl must exist");
    assert.ok(existsSync(summaryFile), "adapter_summary.json must exist");

    const eventsContent = readFileSync(eventsFile, "utf8");
    const callsContent = readFileSync(callsFile, "utf8");
    const summaryContent = readFileSync(summaryFile, "utf8");

    // Verify no secrets logged
    assert.ok(!eventsContent.includes(secretMarker), "adapter_events.jsonl must NOT log prompt secrets");
    assert.ok(!callsContent.includes(secretMarker), "ollama_calls.jsonl must NOT log prompt secrets");
    assert.ok(!summaryContent.includes(secretMarker), "adapter_summary.json must NOT log prompt secrets");

    // Verify calls log
    const callLines = callsContent.trim().split("\n").map((l) => JSON.parse(l));
    assert.equal(callLines.length, 3, "Must log exactly 3 unique Ollama HTTP calls");
    for (let i = 0; i < callLines.length; i++) {
      const call = callLines[i];
      assert.equal(call.call_index, i + 1);
      assert.equal(call.model, "qwen3.5:9b");
      assert.equal(call.bounded_num_predict, 512);
      assert.equal(call.http_status, 200);
      assert.equal(call.usage.input, 120);
      assert.equal(call.usage.output, 45);
      assert.equal(call.stop_reason, "toolUse");
      assert.ok(typeof call.latency_ms === "number");
    }

    // Verify summary
    const summary = JSON.parse(summaryContent);
    assert.equal(summary.summary_schema_version, 1);
    assert.equal(summary.model, "qwen3.5:9b");
    assert.equal(summary.unique_ollama_calls_count, 3);
    assert.equal(summary.unique_interactions_count, 3);
    assert.equal(summary.cache_hits_count, 1);
    assert.equal(summary.errors_count, 2); // 4th cap reached + corrupt digest
    assert.equal(summary.status, "closed");

    // Verify events log contains framed_error events
    const eventLines = eventsContent.trim().split("\n").map((l) => JSON.parse(l));
    const budgetError = eventLines.find((e) => e.event === "framed_error" && e.error_code === "INTERACTION_BUDGET_EXHAUSTED");
    assert.ok(budgetError, "Must log framed_error event for budget exhaustion");

    const adapterError = eventLines.find((e) => e.event === "framed_error" && e.error_code === "MODEL_ADAPTER_ERROR");
    assert.ok(adapterError, "Must log framed_error event for request digest error");

    const cacheHitEvent = eventLines.find((e) => e.event === "cache_hit");
    assert.ok(cacheHitEvent, "Must log cache_hit event");

    console.log("=== ALL AUDITED ADAPTER TESTS PASSED! ===");
  } finally {
    try {
      rmSync(testDir, { recursive: true, force: true });
    } catch {
      // ignore cleanup errors
    }
  }
}

runTests().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
