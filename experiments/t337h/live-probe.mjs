// Two bounded, observation-only local model calls. Never executes returned tools.
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { OllamaModelAdapter, canonicalJson, computeSha256 } from "../../kernel/carrier/pi/host/ollama_model_adapter.mjs";

const root = new URL("./", import.meta.url);
const output = new URL("live-results.json", root);
assert.ok(!existsSync(output), "Refuse accidental rerun: live-results.json already exists");
const modelDigest = "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7";
const info = await (await fetch("http://127.0.0.1:11434/api/tags", { signal: AbortSignal.timeout(5000) })).json();
assert.equal(info.models.find((model) => model.name === "qwen3.5:9b")?.digest, modelDigest);
const version = await (await fetch("http://127.0.0.1:11434/api/version", { signal: AbortSignal.timeout(5000) })).json();
const archived = JSON.parse(readFileSync(new URL("inputs/after-read-request.json", root), "utf8"));
const report = {
  kind: "two_local_observation_only_calls_not_castor_end_to_end",
  started_at: new Date().toISOString(), platform: process.platform, node: process.version,
  ollama_version: version.version, model_digest: modelDigest,
  controls: { max_calls: 2, max_tokens_each: 512, timeout_ms_each: 120000, temperature: 0, seed: 337, source_request_sha256: computeSha256(canonicalJson(archived)), system_prompt_fix: false },
  calls: [],
};
writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
let mode;
const adapter = new OllamaModelAdapter({
  maxInteractions: 2, maxTokens: 512,
  fetchFn: async (url, options) => {
    const payload = JSON.parse(options.body);
    payload.options.temperature = 0;
    payload.options.seed = 337;
    const record = { mode, started_at: new Date().toISOString(), payload };
    report.calls.push(record);
    writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
    const started = Date.now();
    try {
      const response = await fetch(url, { ...options, body: JSON.stringify(payload), signal: AbortSignal.timeout(120000) });
      record.http_status = response.status;
      record.response = await response.json();
      record.latency_ms = Date.now() - started;
      return { ok: response.ok, status: response.status, json: async () => record.response, text: async () => JSON.stringify(record.response) };
    } catch (error) { record.error = String(error); throw error; }
    finally { writeFileSync(output, JSON.stringify(report, null, 2) + "\n"); }
  },
});
for (mode of ["raw_diff", "structured_replacement"]) {
  const request = structuredClone(archived);
  request.interaction_id = `t337h-${mode}`;
  request.parameters.max_tokens = 512;
  if (mode === "structured_replacement") {
    const edit = request.tools.find((tool) => tool.name === "castor_edit_file");
    edit.description = "Submit one terminal edit candidate using exact text replacements. Each oldText must match one unique, non-overlapping region of the original file. Castor verifies and settles it after this agent session ends; do not read or edit again.";
    edit.parameters = {
      type: "object", properties: {
        path: { type: "string" },
        edits: { type: "array", minItems: 1, items: {
          type: "object", properties: { oldText: { type: "string" }, newText: { type: "string" } }, required: ["oldText", "newText"], additionalProperties: false,
        } },
      }, required: ["path", "edits"], additionalProperties: false,
    };
    // Keep the archived transcript internally consistent; current adapter only
    // sends request.tools, and preserves the same empty system content in A/B.
    for (const message of request.messages) {
      if (message.toolsAdded) message.toolsAdded = request.tools;
    }
  }
  const envelope = { interaction_id: request.interaction_id, request_digest: computeSha256(canonicalJson(request)), request };
  try {
    const result = await adapter.handleEnvelope(envelope);
    report.calls.at(-1).converted = JSON.parse(Buffer.from(result.content).toString("utf8"));
  } catch (error) { report.failure = String(error); break; }
  writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ mode, latency_ms: report.calls.at(-1).latency_ms, output_tokens: report.calls.at(-1).response?.eval_count, result: report.calls.at(-1).converted }));
}
report.finished_at = new Date().toISOString();
writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
console.log(`Evidence: ${fileURLToPath(output)}`);
if (report.failure) process.exitCode = 1;
