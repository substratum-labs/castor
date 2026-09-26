#!/usr/bin/env node
/**
 * Audited Host-Local Model Adapter Wrapper for CASTOR_MODEL_SOCKET.
 *
 * Wraps versioned OllamaModelAdapter from kernel/carrier/pi/host/ollama_model_adapter.mjs.
 * Strictly enforces and records:
 * 1. adapter_events.jsonl: Line-by-line audit stream of all adapter lifecycle events,
 *    envelopes received, bounded num_predict, cache hits, and wire-framed errors.
 * 2. ollama_calls.jsonl: Line-by-line log of each unique Ollama HTTP call,
 *    recording model, bounded num_predict, HTTP status, usage, stop reason, and latency.
 * 3. adapter_summary.json: Structured summary of all calls and interactions.
 * 4. Zero secrets logged: Never logs message content, tool arguments, or environment tokens.
 */

import { appendFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  OllamaModelAdapter,
  MAX_INTERACTIONS,
  MAX_TOKENS_LIMIT,
  REQUIRED_MODEL,
  OLLAMA_ENDPOINT,
} from "../kernel/carrier/pi/host/ollama_model_adapter.mjs";

export class AuditedOllamaModelAdapter extends OllamaModelAdapter {
  constructor(options = {}) {
    const evidenceDir = options.evidenceDir || process.env.EVIDENCE_DIR || resolve(process.cwd(), "evidence");
    mkdirSync(evidenceDir, { recursive: true });

    const callsList = [];
    let callCounter = 0;

    const baseFetch = options.fetchFn || globalThis.fetch;

    const auditedFetch = async (url, fetchOptions) => {
      const callIndex = ++callCounter;
      const startTime = Date.now();
      let requestedModel = null;
      let boundedNumPredict = null;

      try {
        if (fetchOptions?.body) {
          const parsed = JSON.parse(fetchOptions.body);
          requestedModel = parsed.model;
          boundedNumPredict = parsed.options?.num_predict;
        }
      } catch {
        // Body was not JSON or inaccessible
      }

      this.logEvent("ollama_http_dispatched", {
        call_index: callIndex,
        model: requestedModel,
        bounded_num_predict: boundedNumPredict,
      });

      let response;
      let latencyMs = 0;
      try {
        response = await baseFetch(url, fetchOptions);
        latencyMs = Date.now() - startTime;
      } catch (err) {
        latencyMs = Date.now() - startTime;
        const errMessage = err instanceof Error ? err.message : String(err);
        const errRecord = {
          call_index: callIndex,
          timestamp: new Date().toISOString(),
          model: requestedModel,
          bounded_num_predict: boundedNumPredict,
          http_status: 0,
          error: errMessage,
          latency_ms: latencyMs,
        };
        callsList.push(errRecord);
        this.logCall(errRecord);
        this.writeSummary();
        throw err;
      }

      if (!response.ok) {
        latencyMs = Date.now() - startTime;
        const clone = response.clone();
        const errBody = await clone.text().catch(() => "");
        const errRecord = {
          call_index: callIndex,
          timestamp: new Date().toISOString(),
          model: requestedModel,
          bounded_num_predict: boundedNumPredict,
          http_status: response.status,
          error: `HTTP ${response.status}: ${errBody.slice(0, 200)}`,
          latency_ms: latencyMs,
        };
        callsList.push(errRecord);
        this.logCall(errRecord);
        this.writeSummary();
        return response;
      }

      const clone = response.clone();
      let data = {};
      try {
        data = await clone.json();
      } catch {
        // Unable to clone/parse JSON
      }

      const inputTokens = Number(data.prompt_eval_count || 0);
      const outputTokens = Number(data.eval_count || 0);
      const hasToolCalls = Array.isArray(data.message?.tool_calls) && data.message.tool_calls.length > 0;
      const stopReason = hasToolCalls
        ? "toolUse"
        : data.done_reason === "length"
          ? "length"
          : "stop";

      const callRecord = {
        call_index: callIndex,
        timestamp: new Date().toISOString(),
        model: requestedModel,
        bounded_num_predict: boundedNumPredict,
        http_status: response.status,
        usage: {
          input: inputTokens,
          output: outputTokens,
        },
        stop_reason: stopReason,
        latency_ms: latencyMs,
      };

      callsList.push(callRecord);
      this.logCall(callRecord);

      this.logEvent("ollama_http_completed", {
        call_index: callIndex,
        http_status: response.status,
        bounded_num_predict: boundedNumPredict,
        input_tokens: inputTokens,
        output_tokens: outputTokens,
        stop_reason: stopReason,
        latency_ms: latencyMs,
      });

      this.writeSummary();
      return response;
    };

    super({
      ...options,
      fetchFn: auditedFetch,
    });

    this.evidenceDir = evidenceDir;
    this.eventsPath = join(this.evidenceDir, "adapter_events.jsonl");
    this.callsPath = join(this.evidenceDir, "ollama_calls.jsonl");
    this.summaryPath = join(this.evidenceDir, "adapter_summary.json");

    this.envelopesReceived = 0;
    this.cacheHits = 0;
    this.errorsCount = 0;
    this.callsList = callsList;
    this.interactionsList = [];

    // Ensure empty evidence files exist immediately on startup
    writeFileSync(this.eventsPath, "", { flag: "a" });
    writeFileSync(this.callsPath, "", { flag: "a" });
    this.writeSummary("initialized");
  }

  logEvent(event, data = {}) {
    const entry = {
      timestamp: new Date().toISOString(),
      event,
      ...data,
    };
    try {
      appendFileSync(this.eventsPath, JSON.stringify(entry) + "\n", "utf8");
    } catch {
      // Ignore disk append errors in non-blocking audit logger
    }
  }

  logCall(callRecord) {
    try {
      appendFileSync(this.callsPath, JSON.stringify(callRecord) + "\n", "utf8");
    } catch {
      // Ignore disk append errors
    }
  }

  writeSummary(status = "running") {
    const summary = {
      summary_schema_version: 1,
      model: this.model,
      max_interactions_limit: this.maxInteractions,
      max_tokens_limit: this.maxTokens,
      total_envelopes_received: this.envelopesReceived,
      unique_interactions_count: new Set(this.interactionsList.map((i) => i.interaction_id)).size,
      unique_ollama_calls_count: this.callsList.length,
      cache_hits_count: this.cacheHits,
      errors_count: this.errorsCount,
      calls: this.callsList,
      interactions: this.interactionsList,
      status,
    };
    try {
      writeFileSync(this.summaryPath, JSON.stringify(summary, null, 2) + "\n", "utf8");
    } catch {
      // Ignore disk write errors
    }
  }

  async handleEnvelope(envelope) {
    this.envelopesReceived += 1;
    const interactionId = envelope?.interaction_id;
    const requestDigest = envelope?.request_digest;
    const messagesCount = Array.isArray(envelope?.request?.messages) ? envelope.request.messages.length : 0;
    const toolsCount = Array.isArray(envelope?.request?.tools) ? envelope.request.tools.length : 0;
    const requestedMaxTokens = envelope?.request?.parameters?.max_tokens;
    const boundedNumPredict =
      typeof requestedMaxTokens === "number" && Number.isInteger(requestedMaxTokens) && requestedMaxTokens > 0
        ? Math.min(requestedMaxTokens, this.maxTokens)
        : this.maxTokens;

    if (interactionId && this.digests.has(interactionId) && this.cache.has(interactionId)) {
      this.cacheHits += 1;
      this.logEvent("cache_hit", {
        interaction_id: interactionId,
        request_digest: requestDigest,
      });
    }

    this.logEvent("envelope_received", {
      interaction_id: interactionId,
      request_digest: requestDigest,
      message_count: messagesCount,
      tool_count: toolsCount,
      requested_max_tokens: requestedMaxTokens ?? null,
      bounded_num_predict: boundedNumPredict,
    });

    try {
      const response = await super.handleEnvelope(envelope);

      let stopReason = "unknown";
      let usage = { input: 0, output: 0 };
      if (response && response.content) {
        try {
          const inner = JSON.parse(Buffer.from(response.content).toString("utf8"));
          stopReason = inner.stopReason || stopReason;
          usage = inner.usage || usage;
        } catch {
          // unable to parse inner content
        }
      }

      this.interactionsList.push({
        interaction_id: interactionId,
        request_digest: requestDigest,
        bounded_num_predict: boundedNumPredict,
        stop_reason: stopReason,
        usage,
        observation_digest: response.observation_digest,
      });

      this.logEvent("envelope_completed", {
        interaction_id: interactionId,
        observation_digest: response.observation_digest,
        stop_reason: stopReason,
        usage,
      });

      this.writeSummary("running");
      return response;
    } catch (err) {
      this.errorsCount += 1;
      const message = err instanceof Error ? err.message : String(err);
      const errorCode = message.startsWith("Interaction cap reached:")
        ? "INTERACTION_BUDGET_EXHAUSTED"
        : "MODEL_ADAPTER_ERROR";

      this.logEvent("framed_error", {
        interaction_id: interactionId,
        error_code: errorCode,
        error_message: message,
      });

      this.writeSummary("running");
      throw err;
    }
  }

  async close() {
    this.writeSummary("closed");
    this.logEvent("adapter_stopped", {
      total_envelopes: this.envelopesReceived,
      unique_interactions: new Set(this.interactionsList.map((i) => i.interaction_id)).size,
      unique_calls: this.callsList.length,
      errors: this.errorsCount,
    });
    return super.close();
  }
}

// Standalone execution entrypoint
if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const socketPath = process.argv[2] || process.env.CASTOR_MODEL_SOCKET;
  const evidenceDir = process.argv[3] || process.env.EVIDENCE_DIR;

  if (!socketPath) {
    console.error("Usage: node audited_adapter_wrapper.mjs <socket_path> [evidence_dir]");
    console.error("Or set CASTOR_MODEL_SOCKET and EVIDENCE_DIR environment variables");
    process.exit(1);
  }

  const adapter = new AuditedOllamaModelAdapter({ evidenceDir });
  adapter.logEvent("adapter_started", {
    socket_path: socketPath,
    model: adapter.model,
    max_tokens: adapter.maxTokens,
    max_interactions: adapter.maxInteractions,
  });

  adapter
    .listen(socketPath)
    .then(() => {
      console.log(`Audited Ollama model adapter listening on ${socketPath}`);
      console.log(`Evidence directory: ${adapter.evidenceDir}`);
      console.log(`Target: ${OLLAMA_ENDPOINT}, model: ${REQUIRED_MODEL}, max_tokens <= ${MAX_TOKENS_LIMIT}`);
    })
    .catch((err) => {
      console.error("Failed to start audited adapter:", err);
      process.exit(1);
    });

  const cleanup = () => {
    adapter.close().then(() => process.exit(0));
  };
  process.on("SIGINT", cleanup);
  process.on("SIGTERM", cleanup);
}
