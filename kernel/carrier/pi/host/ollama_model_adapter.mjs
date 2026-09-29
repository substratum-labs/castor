#!/usr/bin/env node
/**
 * Host-Local Model Adapter for CASTOR_MODEL_SOCKET.
 *
 * Enforces:
 * 1. Wire framing: 4-byte big-endian length prefix framing.
 * 2. Canonical JSON and SHA-256 request digest verification against Castor envelope.
 * 3. Interaction bounds: Max 3 unique interaction IDs. Extra unique IDs are refused.
 * 4. Idempotency: Same-ID retries return cached observation without duplicate inference.
 * 5. Local Ollama bounds: Strictly routes to http://127.0.0.1:11434/api/chat.
 * 6. Model pin: Strictly forces model to qwen3.5:9b.
 * 7. Token cap: Strictly caps num_predict <= 512.
 * 8. Response conversion: Converts Ollama response into Castor observation envelope.
 */

import { createHash } from "node:crypto";
import { createServer, createConnection } from "node:net";
import { unlinkSync, existsSync } from "node:fs";

export const MAX_FRAME_BYTES = 16 * 1024 * 1024;
export const MAX_INTERACTIONS = 3;
export const MAX_TOKENS_LIMIT = 512;
export const REQUIRED_MODEL = "qwen3.5:9b";
export const OLLAMA_HOST = "127.0.0.1:11434";
export const OLLAMA_PATH = "/api/chat";
export const OLLAMA_ENDPOINT = `http://${OLLAMA_HOST}${OLLAMA_PATH}`;

export async function readBoundedJson(response, cap = 2 * 1024 * 1024) {
  if (response.body?.getReader) {
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > cap) {
        await reader.cancel();
        throw new Error("provider response exceeds cap");
      }
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  }
  // Fetch mocks in older adapter tests expose only json().
  const value = await response.json();
  if (Buffer.byteLength(JSON.stringify(value)) > cap) throw new Error("provider response exceeds cap");
  return value;
}

/** Read bounded loopback metadata without performing inference. */
export async function readLocalModelPin(fetchFn = globalThis.fetch) {
  async function read(path) {
    const response = await fetchFn(`http://${OLLAMA_HOST}${path}`, {
      method: "GET", redirect: "error", signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) throw new Error(`model metadata HTTP ${response.status}`);
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      let bytes = 0;
      const chunks = [];
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.length;
        if (bytes > 1024 * 1024) { await reader.cancel(); throw new Error("model metadata exceeds cap"); }
        chunks.push(value);
      }
      return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    }
    const value = await response.json(); // mocked fetch response in unit tests
    if (Buffer.byteLength(JSON.stringify(value)) > 1024 * 1024) throw new Error("model metadata exceeds cap");
    return value;
  }
  const tags = await read("/api/tags");
  const matches = Array.isArray(tags.models) ? tags.models.filter((m) => m.name === REQUIRED_MODEL) : [];
  if (matches.length !== 1 || !/^[0-9a-f]{64}$/.test(matches[0].digest)) {
    throw new Error("local model metadata missing or ambiguous");
  }
  const version = (await read("/api/version")).version;
  if (typeof version !== "string" || !version || version.length > 64) {
    throw new Error("invalid local model version metadata");
  }
  return { name: REQUIRED_MODEL, digest: matches[0].digest, version };
}

function sortedValue(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(sortedValue);
  if (typeof value === "object" && value.constructor === Object) {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) result[key] = sortedValue(value[key]);
    }
    return result;
  }
  throw new TypeError("model request must contain only finite JSON values");
}

export function canonicalJson(value) {
  return JSON.stringify(sortedValue(value));
}

export function computeSha256(data) {
  const bytes = typeof data === "string" ? Buffer.from(data, "utf8") : data;
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

export function formatMessagesForOllama(messages) {
  if (!Array.isArray(messages)) return [];
  let firstSystem = true;
  return messages.map((msg) => {
    if (msg.role === "system") {
      const content = typeof msg.content === "string" ? msg.content : Array.isArray(msg.content)
        ? msg.content.filter((block) => block?.type === "text").map((block) => block.text).join("\n") : "";
      const sections = msg.sections && typeof msg.sections === "object" ? msg.sections : {};
      if (firstSystem) {
        firstSystem = false;
        return { role: "system", content: [content, ...Object.values(sections).filter((value) => value !== null)].filter((value) => value.length > 0).join("\n\n") };
      }
      const updates = Object.entries(sections).map(([name, value]) => value === null
        ? `Removed system prompt section "${name}".`
        : `Updated system prompt section "${name}":\n\n${value}`);
      return { role: "system", content: [content, ...updates].filter((part) => part.length > 0).join("\n\n") };
    }
    if (msg.role === "user") {
      let content = "";
      if (typeof msg.content === "string") {
        content = msg.content;
      } else if (Array.isArray(msg.content)) {
        content = msg.content
          .filter((b) => b && b.type === "text")
          .map((b) => b.text || "")
          .join("\n");
      }
      return { role: "user", content };
    }
    if (msg.role === "assistant") {
      let content = "";
      const toolCalls = [];
      if (typeof msg.content === "string") {
        content = msg.content;
      } else if (Array.isArray(msg.content)) {
        content = msg.content
          .filter((b) => b && b.type === "text")
          .map((b) => b.text || "")
          .join("\n");
        for (const b of msg.content) {
          if (b && b.type === "toolCall") {
            toolCalls.push({
              id: b.id,
              type: "function",
              function: {
                name: b.name,
                arguments: b.arguments || {},
              },
            });
          }
        }
      }
      if (Array.isArray(msg.tool_calls) && toolCalls.length === 0) {
        for (const tc of msg.tool_calls) {
          toolCalls.push(tc);
        }
      }
      const assistantMsg = { role: "assistant", content };
      if (toolCalls.length > 0) assistantMsg.tool_calls = toolCalls;
      return assistantMsg;
    }
    if (msg.role === "toolResult" || msg.role === "tool") {
      let content = "";
      if (typeof msg.content === "string") {
        content = msg.content;
      } else if (Array.isArray(msg.content)) {
        content = msg.content
          .map((b) => (typeof b?.text === "string" ? b.text : JSON.stringify(b)))
          .join("\n");
      } else {
        content = JSON.stringify(msg.content ?? "");
      }
      const toolMsg = { role: "tool", content };
      const toolCallId = msg.toolCallId || msg.tool_call_id;
      if (toolCallId) {
        toolMsg.tool_call_id = String(toolCallId);
      }
      const toolName = msg.toolName || msg.tool_name || msg.name;
      if (toolName) {
        toolMsg.tool_name = String(toolName);
      }
      return toolMsg;
    }
    return {
      role: msg.role || "user",
      content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content ?? ""),
    };
  });
}

export function formatToolsForOllama(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return undefined;
  return tools.map((tool) => {
    if (tool.type === "function" && tool.function) return tool;
    return {
      type: "function",
      function: {
        name: tool.name,
        description: tool.description || "",
        parameters: tool.parameters || {},
      },
    };
  });
}

export function transformOllamaResponse(ollamaData, interactionId, { strict = false } = {}) {
  if (strict) {
    if (ollamaData?.model !== REQUIRED_MODEL) throw new Error("model identity mismatch");
    if (ollamaData.done !== true) throw new Error("unfinished model response");
    if (!Number.isSafeInteger(ollamaData.prompt_eval_count) ||
        !Number.isSafeInteger(ollamaData.eval_count) ||
        ollamaData.prompt_eval_count < 0 ||
        ollamaData.eval_count < 0 ||
        ollamaData.eval_count > MAX_TOKENS_LIMIT) {
      throw new Error("model usage missing or outside output budget");
    }
  }
  const message = ollamaData.message || {};
  const contentBlocks = [];

  if (message.content && typeof message.content === "string" && message.content.trim().length > 0) {
    contentBlocks.push({ type: "text", text: message.content });
  }

  if (Array.isArray(message.tool_calls) && message.tool_calls.length > 0) {
    for (let i = 0; i < message.tool_calls.length; i++) {
      const tc = message.tool_calls[i];
      const fn = tc.function || tc;
      let args = fn.arguments;
      if (typeof args === "string") {
        try {
          args = JSON.parse(args);
        } catch {
          args = { raw: args };
        }
      }
      contentBlocks.push({
        type: "toolCall",
        id: tc.id || `call-${interactionId}-${i + 1}`,
        name: fn.name,
        arguments: args || {},
      });
    }
  }

  if (contentBlocks.length === 0) {
    contentBlocks.push({ type: "text", text: "" });
  }

  const hasToolCalls = contentBlocks.some((b) => b.type === "toolCall");
  const stopReason = hasToolCalls
    ? "toolUse"
    : ollamaData.done_reason === "length"
      ? "length"
      : "stop";

  const innerResult = {
    content: contentBlocks,
    stopReason,
    usage: {
      input: Number(ollamaData.prompt_eval_count || 0),
      output: Number(ollamaData.eval_count || 0),
    },
  };

  const innerBytes = Buffer.from(JSON.stringify(innerResult), "utf8");
  const observationDigest = computeSha256(innerBytes);

  return {
    interaction_id: interactionId,
    observation_region_id: `region://model-observation/${interactionId}`,
    observation_digest: observationDigest,
    content: Array.from(innerBytes),
  };
}

export class OllamaModelAdapter {
  constructor(options = {}) {
    this.maxInteractions = options.maxInteractions ?? MAX_INTERACTIONS;
    this.maxTokens = options.maxTokens ?? MAX_TOKENS_LIMIT;
    this.model = options.model ?? REQUIRED_MODEL;
    this.ollamaEndpoint = options.ollamaEndpoint ?? OLLAMA_ENDPOINT;
    this.fetchFn = options.fetchFn ?? globalThis.fetch;
    this.cache = new Map();
    this.digests = new Map();
    this.inFlight = new Map();
    this.server = null;
    this.socketPath = null;
    this.callCount = 0;
    this.strictPolicy = options.strictPolicy ?? false;
    this.expectedDigest = options.expectedDigest ?? null;
    this.pinChecker = options.pinChecker ?? readLocalModelPin;

    if (
      typeof this.maxInteractions !== "number" ||
      !Number.isInteger(this.maxInteractions) ||
      this.maxInteractions <= 0
    ) {
      throw new Error(
        `Invalid maxInteractions option: expected finite positive integer, got ${this.maxInteractions}`
      );
    }

    if (
      typeof this.maxTokens !== "number" ||
      !Number.isInteger(this.maxTokens) ||
      this.maxTokens <= 0
    ) {
      throw new Error(
        `Invalid maxTokens option: expected finite positive integer, got ${this.maxTokens}`
      );
    }

    // Pin endpoint exact protocol, host, and path
    const url = new URL(this.ollamaEndpoint);
    if (
      url.protocol !== "http:" ||
      url.host !== OLLAMA_HOST ||
      url.pathname !== OLLAMA_PATH ||
      url.search !== "" ||
      url.hash !== ""
    ) {
      throw new Error(
        `Security violation: endpoint must strictly connect to http://${OLLAMA_HOST}${OLLAMA_PATH}`
      );
    }
  }

  async handleEnvelope(envelope) {
    if (!envelope || typeof envelope !== "object") {
      throw new Error("Invalid envelope: must be an object");
    }
    const interactionId = envelope.interaction_id;
    if (!interactionId || typeof interactionId !== "string") {
      throw new Error("Invalid envelope: missing interaction_id");
    }

    // Request Envelope Validation
    const expectedDigest = envelope.request_digest;
    if (!expectedDigest || typeof expectedDigest !== "string") {
      throw new Error("Invalid envelope: missing request_digest");
    }
    const request = envelope.request;
    if (!request || typeof request !== "object") {
      throw new Error("Invalid envelope: missing request object");
    }
    if (request.schema_version !== 1) {
      throw new Error(`Invalid request schema_version: expected 1, got ${request.schema_version}`);
    }
    if (request.interaction_id !== interactionId) {
      throw new Error(
        `Interaction ID mismatch: envelope=${interactionId}, request=${request.interaction_id}`
      );
    }
    if (!Array.isArray(request.messages) || request.messages.length === 0) {
      throw new Error("Invalid request: messages must be a non-empty array");
    }
    if (!Array.isArray(request.tools)) {
      throw new Error("Invalid request: tools must be an array");
    }

    // (c) Validate requested max_tokens as a finite positive integer before capping to 512
    let boundedTokens = this.maxTokens;
    if (
      request.parameters &&
      "max_tokens" in request.parameters &&
      request.parameters.max_tokens !== undefined &&
      request.parameters.max_tokens !== null
    ) {
      const requested = request.parameters.max_tokens;
      if (typeof requested !== "number" || !Number.isInteger(requested) || requested <= 0) {
        throw new Error(
          `Invalid max_tokens: expected finite positive integer, got ${JSON.stringify(requested)}`
        );
      }
      boundedTokens = Math.min(requested, this.maxTokens);
    }

    // Canonical Digest Verification
    const canonJson = canonicalJson(request);
    const computedDigest = computeSha256(canonJson);
    if (computedDigest !== expectedDigest) {
      throw new Error(
        `Request digest mismatch: expected ${expectedDigest}, computed ${computedDigest}`
      );
    }

    // (a) Check same-ID retry against original stored digest:
    // If interaction_id was already seen (either completed or in-flight), reject mismatched digest!
    if (this.digests.has(interactionId)) {
      const originalDigest = this.digests.get(interactionId);
      if (expectedDigest !== originalDigest) {
        throw new Error(
          `Interaction ID reuse with mismatched request digest: expected ${originalDigest}, got ${expectedDigest}`
        );
      }
      // If completed, return cached response
      if (this.cache.has(interactionId)) {
        return this.cache.get(interactionId);
      }
      // If currently in-flight, await and return the in-flight promise
      if (this.inFlight.has(interactionId)) {
        return await this.inFlight.get(interactionId);
      }
    }

    // (b) Interaction Cap Check:
    // Count in-flight + completed unique IDs toward the cap
    const uniqueIds = new Set([...this.cache.keys(), ...this.inFlight.keys()]);
    if (uniqueIds.size >= this.maxInteractions) {
      throw new Error(
        `Interaction cap reached: maximum ${this.maxInteractions} unique interactions allowed; refusing ${interactionId}`
      );
    }

    // Build Ollama Request enforcing model & token caps
    const ollamaPayload = {
      model: this.model,
      messages: formatMessagesForOllama(request.messages),
      stream: false,
      options: {
        num_predict: boundedTokens,
      },
    };
    const tools = formatToolsForOllama(request.tools);
    if (tools && tools.length > 0) {
      ollamaPayload.tools = tools;
    }

    // (b) Reserve an in-flight promise per interaction before awaiting fetch
    this.digests.set(interactionId, expectedDigest);

    const executeInference = async () => {
      if (this.expectedDigest) {
        const pin = await this.pinChecker();
        if (pin.digest !== this.expectedDigest || pin.name !== this.model) {
          throw new Error("local model digest changed before provider call");
        }
      }
      this.callCount++;
      // (d) Set redirect: "error" so a local Ollama response cannot redirect outside loopback
      const response = await this.fetchFn(this.ollamaEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ollamaPayload),
        redirect: "error",
        signal: AbortSignal.timeout(120_000),
      });

      if (!response.ok) {
        throw new Error(`Ollama API error: status ${response.status} ${response.statusText}`);
      }

      const ollamaJson = await readBoundedJson(response);
      if (this.expectedDigest) {
        const pin = await this.pinChecker();
        if (pin.digest !== this.expectedDigest || pin.name !== this.model) {
          throw new Error("local model digest changed after provider call");
        }
      }
      const outerResponse = transformOllamaResponse(ollamaJson, interactionId, { strict: this.strictPolicy });
      this.cache.set(interactionId, outerResponse);
      return outerResponse;
    };

    const inFlightPromise = executeInference();
    this.inFlight.set(interactionId, inFlightPromise);

    try {
      return await inFlightPromise;
    } catch (err) {
      // If execution failed before completion, remove stored digest so future retries can succeed
      if (!this.cache.has(interactionId)) {
        this.digests.delete(interactionId);
      }
      throw err;
    } finally {
      this.inFlight.delete(interactionId);
    }
  }

  listen(socketPath) {
    this.socketPath = socketPath;
    if (existsSync(socketPath)) {
      try {
        unlinkSync(socketPath);
      } catch {
        // ignore
      }
    }

    this.server = createServer((stream) => {
      let buffer = Buffer.alloc(0);
      stream.on("data", async (chunk) => {
        buffer = Buffer.concat([buffer, chunk]);
        while (buffer.length >= 4) {
          const frameLength = buffer.readUInt32BE(0);
          if (frameLength === 0 || frameLength > MAX_FRAME_BYTES) {
            stream.end();
            return;
          }
          if (buffer.length < 4 + frameLength) {
            // Wait for full frame to arrive
            return;
          }
          const framePayload = buffer.subarray(4, 4 + frameLength);
          buffer = buffer.subarray(4 + frameLength);

          try {
            const envelope = JSON.parse(framePayload.toString("utf8"));
            const responseObj = await this.handleEnvelope(envelope);
            const responseBytes = Buffer.from(JSON.stringify(responseObj), "utf8");
            const header = Buffer.alloc(4);
            header.writeUInt32BE(responseBytes.length, 0);
            stream.write(Buffer.concat([header, responseBytes]));
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            const responseBytes = Buffer.from(JSON.stringify({
              error: {
                code: message.startsWith("Interaction cap reached:")
                  ? "INTERACTION_BUDGET_EXHAUSTED"
                  : "MODEL_ADAPTER_ERROR",
                message,
              },
            }), "utf8");
            const header = Buffer.alloc(4);
            header.writeUInt32BE(responseBytes.length, 0);
            stream.write(Buffer.concat([header, responseBytes]));
          }
        }
      });
    });

    return new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(socketPath, () => resolve(this.server));
    });
  }

  close() {
    return new Promise((resolve) => {
      if (this.server) {
        this.server.close(() => {
          if (this.socketPath && existsSync(this.socketPath)) {
            try {
              unlinkSync(this.socketPath);
            } catch {
              // ignore
            }
          }
          resolve();
        });
      } else {
        resolve();
      }
    });
  }
}

// Standalone execution entrypoint
if (import.meta.url === `file://${process.argv[1]}`) {
  const socketPath = process.argv[2] || process.env.CASTOR_MODEL_SOCKET;
  if (!socketPath) {
    console.error("Usage: node ollama_model_adapter.mjs <socket_path>");
    console.error("Or set CASTOR_MODEL_SOCKET environment variable");
    process.exit(1);
  }

  const adapter = new OllamaModelAdapter({
    strictPolicy: process.env.CASTOR_LOCAL_DEVELOPER_MODE === "1",
    expectedDigest: process.env.CASTOR_EXPECTED_MODEL_DIGEST || null,
  });
  adapter
    .listen(socketPath)
    .then(() => {
      console.log(`Ollama model adapter listening on ${socketPath}`);
      console.log(`Target: ${OLLAMA_ENDPOINT}, model: ${REQUIRED_MODEL}, max_tokens <= ${MAX_TOKENS_LIMIT}`);
    })
    .catch((err) => {
      console.error("Failed to start adapter:", err);
      process.exit(1);
    });

  const cleanup = () => {
    adapter.close().then(() => process.exit(0));
  };
  process.on("SIGINT", cleanup);
  process.on("SIGTERM", cleanup);
}
