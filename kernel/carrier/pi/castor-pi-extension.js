import { createHash } from "node:crypto";
import { readFile, realpath } from "node:fs/promises";
import { resolve, relative, isAbsolute } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { Type, createAssistantMessageEventStream, getCurrentTools } from "@earendil-works/pi-ai";
import { AisaClient, canonicalJson } from "./protocol.js";
import { compileExactEdits } from "./edit-candidate.js";

const EMPTY_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const WORKSPACE = "/workspace";
const sha256 = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;

// This is cooperative feedback for Pi, not an authority check. The trusted
// actuator independently checks the exact patch before touching the workspace.
function checkPatchShape(path, patch) {
  if (typeof patch !== "string") throw new Error("patch must be a unified diff");
  const lines = patch.split("\n");
  if (lines.at(-1) === "") lines.pop();
  let cursor = lines[0] === `diff --git a/${path} b/${path}` ? 1 : 0;
  if (lines[cursor++] !== `--- a/${path}` || lines[cursor++] !== `+++ b/${path}`) {
    throw new Error("patch must target exactly the requested file");
  }
  let hunks = 0;
  while (cursor < lines.length) {
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$/.exec(lines[cursor++]);
    if (!header) throw new Error("invalid patch hunk header");
    const oldCount = Number(header[2] ?? 1);
    const newCount = Number(header[4] ?? 1);
    let oldSeen = 0;
    let newSeen = 0;
    while (cursor < lines.length && !lines[cursor].startsWith("@@ ")) {
      const line = lines[cursor++];
      if (line === "\\ No newline at end of file") continue;
      if (line.startsWith(" ")) { oldSeen++; newSeen++; }
      else if (line.startsWith("-")) oldSeen++;
      else if (line.startsWith("+")) newSeen++;
      else throw new Error("invalid patch hunk line");
    }
    if (oldSeen !== oldCount || newSeen !== newCount) {
      throw new Error("patch hunk line count mismatch");
    }
    hunks++;
  }
  if (hunks === 0) throw new Error("patch contains no hunks");
}

function requireOutcome(outcome, expected) {
  if (outcome?.type !== expected) throw new Error(`Castor expected ${expected}, got ${outcome?.type || "no outcome"}`);
  return outcome;
}

async function readWorkspace(path) {
  if (typeof path !== "string" || path.length === 0 || isAbsolute(path)) throw new Error("workspace path must be relative");
  const full = resolve(WORKSPACE, path);
  const relativePath = relative(WORKSPACE, full);
  if (!relativePath || relativePath === ".." || relativePath.startsWith("../")) throw new Error("workspace path escapes snapshot");
  const physical = await realpath(full);
  const physicalRelative = relative(WORKSPACE, physical);
  if (!physicalRelative || physicalRelative === ".." || physicalRelative.startsWith("../")) throw new Error("workspace symlink escapes snapshot");
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await readFile(physical));
}

function emitBuffered(stream, output, result) {
  if (!Array.isArray(result.content) || !["stop", "toolUse", "length"].includes(result.stopReason)) {
    throw new Error("invalid buffered model response");
  }
  stream.push({ type: "start", partial: output });
  for (const item of result.content) {
    const index = output.content.length;
    if (item.type === "text" && typeof item.text === "string") {
      const block = { type: "text", text: "" };
      output.content.push(block);
      stream.push({ type: "text_start", contentIndex: index, partial: output });
      block.text = item.text;
      stream.push({ type: "text_delta", contentIndex: index, delta: item.text, partial: output });
      stream.push({ type: "text_end", contentIndex: index, content: item.text, partial: output });
    } else if (item.type === "toolCall" && typeof item.id === "string" && typeof item.name === "string" && item.arguments && typeof item.arguments === "object") {
      const block = { type: "toolCall", id: item.id, name: item.name, arguments: {} };
      output.content.push(block);
      stream.push({ type: "toolcall_start", contentIndex: index, partial: output });
      const args = canonicalJson(item.arguments);
      block.arguments = item.arguments;
      stream.push({ type: "toolcall_delta", contentIndex: index, delta: args, partial: output });
      stream.push({ type: "toolcall_end", contentIndex: index, toolCall: block, partial: output });
    } else {
      throw new Error("unsupported buffered content block");
    }
  }
  output.usage.input = result.usage?.input || 0;
  output.usage.output = result.usage?.output || 0;
  output.usage.totalTokens = output.usage.input + output.usage.output;
  output.stopReason = result.stopReason;
  stream.push({ type: "done", reason: output.stopReason, message: output });
  stream.end();
}

export default function castorExtension(pi) {
  const ipc = new AisaClient();
  const state = {
    turnId: 1,
    leaseEpoch: 0,
    baseDigest: EMPTY_DIGEST,
    expectedGeneration: 1,
    active: false,
    nextInteraction: 0,
    nextAction: 0,
    lastObservation: null,
    modelBusy: false,
    terminalArmed: false,
  };

  async function ensureTurn() {
    if (state.active) return;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const projection = requireOutcome(
        await ipc.request("ObserveProjection", {}),
        "ProjectionObserved",
      );
      if (
        (projection.projection_digest !== null &&
          (typeof projection.projection_digest !== "string" ||
            !/^sha256:[0-9a-f]{64}$/.test(projection.projection_digest))) ||
        !Number.isSafeInteger(projection.generation) ||
        projection.generation < 1
      ) {
        throw new Error("Castor returned an invalid projection observation");
      }
      if (state.expectedGeneration !== projection.generation) {
        throw new Error("Castor projection generation changed; task is fenced");
      }
      state.baseDigest = projection.projection_digest ?? EMPTY_DIGEST;
      const admitted = await ipc.request("AdmitTurn", {
        agent_id: "agent-1",
        turn_id: state.turnId,
        lease_epoch: 0,
        base_projection_digest: state.baseDigest,
        expected_generation: projection.generation,
      });
      if (admitted?.type === "Admitted") {
        state.active = true;
        state.leaseEpoch = 0;
        return;
      }
      if (admitted?.type !== "RejectedStaleAuthority") {
        requireOutcome(admitted, "Admitted");
      }
      if (attempt < 2) await delay(10 * (attempt + 1));
    }
    throw new Error("Castor projection kept changing during turn admission");
  }

  async function bindBufferedModel(model, context, options) {
    await ensureTurn();
    const interactionId = `interaction-${state.turnId}-${++state.nextInteraction}`;
    const fullRequest = {
      schema_version: 1,
      interaction_id: interactionId,
      model: model.id,
      messages: context.messages,
      tools: getCurrentTools(context.messages),
      parameters: { max_tokens: options?.maxTokens || model.maxTokens },
    };
    const bytes = Buffer.from(canonicalJson(fullRequest));
    if (bytes.length === 0 || bytes.length > 1024 * 1024) throw new Error("model request exceeds Castor Region limit");
    const requestDigest = sha256(bytes);
    const persisted = await ipc.request("EnsureRegion", {
      region_ref: `region://model-request/${interactionId}`,
      content_digest: requestDigest,
      content: [...bytes],
      profile: "D1",
    });
    if (!["Success", "AlreadyPersistedSameContent"].includes(persisted?.type)) throw new Error("model request Region was not persisted");
    requireOutcome(await ipc.request("RequestInteraction", {
      interaction_id: interactionId,
      lease_epoch: state.leaseEpoch,
      request_digest: requestDigest,
    }), "InteractionRequested");
    const nextLease = state.leaseEpoch + 1;
    // The host task watchdog is 300 seconds. Do not abandon a legal model
    // observation before the host can bind it and grant the next lease.
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      if (options?.signal?.aborted) throw new Error("model request aborted");
      const consumed = await ipc.request("ConsumeInteraction", {
        interaction_id: interactionId,
        lease_epoch: nextLease,
      });
      if (consumed?.type === "InteractionConsumed") {
        const observation = consumed.payload;
        const content = Buffer.from(observation.content);
        if (sha256(content) !== observation.observation_digest || observation.interaction_id !== interactionId) {
          throw new Error("bound model observation digest mismatch");
        }
        state.leaseEpoch = nextLease;
        state.lastObservation = {
          region: observation.observation_region_id,
          digest: observation.observation_digest,
        };
        return JSON.parse(content.toString("utf8"));
      }
      if (!["RejectedCurrentState", "RejectedStaleAuthority"].includes(consumed?.type)) {
        throw new Error(`model observation rejected: ${consumed?.type}`);
      }
      await delay(20);
    }
    throw new Error("timed out waiting for durably bound model observation");
  }

  pi.registerProvider("castor", {
    api: "castor-buffered",
    baseUrl: "castor+unix://local",
    apiKey: "castor-local",
    models: [{
      id: "castor-task",
      name: "Castor Task",
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32768,
      maxTokens: 4096,
    }],
    streamSimple(model, context, options) {
      const stream = createAssistantMessageEventStream();
      const output = {
        role: "assistant",
        content: [],
        api: model.api,
        provider: model.provider,
        model: model.id,
        usage: {
          input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        stopReason: "pending",
        timestamp: Date.now(),
      };
      if (state.terminalArmed) {
        queueMicrotask(() => emitBuffered(stream, output, {
          content: [{ type: "text", text: "Patch candidate armed for host settlement. Concluding session." }],
          stopReason: "stop",
          usage: { input: 0, output: 0 },
        }));
        return stream;
      }
      if (state.modelBusy) {
        output.stopReason = "error";
        output.errorMessage = "concurrent model requests are not supported";
        stream.push({ type: "error", reason: "error", error: output });
        stream.end();
        return stream;
      }
      state.modelBusy = true;
      (async () => {
        try {
          const result = await bindBufferedModel(model, context, options);
          emitBuffered(stream, output, result);
        } catch (error) {
          output.stopReason = options?.signal?.aborted ? "aborted" : "error";
          output.errorMessage = error instanceof Error ? error.message : String(error);
          stream.push({ type: "error", reason: output.stopReason, error: output });
          stream.end();
        } finally {
          state.modelBusy = false;
        }
      })();
      return stream;
    },
  });

  pi.registerTool({
    name: "castor_read_file",
    label: "Read file",
    description: "Read a workspace-relative path from the immutable task snapshot.",
    parameters: Type.Object({ path: Type.String() }),
    async execute(_toolCallId, parameters) {
      if (state.terminalArmed) throw new Error("workspace remains immutable and effect is unverified pending host settlement");
      const text = await readWorkspace(parameters.path);
      return { content: [{ type: "text", text }], details: { path: parameters.path } };
    },
  });

  pi.registerTool({
    name: "castor_edit_file",
    label: "Propose edit",
    description: "Submit one terminal edit candidate at a workspace-relative path using exact text replacements. Each oldText must match one unique, non-overlapping region of the original file. Castor verifies and settles it after this agent session ends; do not read or edit again.",
    parameters: Type.Object({
      path: Type.String(),
      edits: Type.Array(Type.Object({ oldText: Type.String(), newText: Type.String() }), { minItems: 1 }),
    }),
    async execute(_toolCallId, parameters) {
      if (!state.active || !state.lastObservation) throw new Error("model result must be bound before editing");
      if (state.nextAction > 0) throw new Error("bounded one-shot task permits one edit action");
      if (typeof parameters.path !== "string" || isAbsolute(parameters.path) || parameters.path.includes("..")) throw new Error("invalid workspace edit path");
      const original = await readWorkspace(parameters.path);
      const { patch } = compileExactEdits(parameters.path, original, parameters.edits);
      checkPatchShape(parameters.path, patch);
      const actionId = `action-${++state.nextAction}`;
      const payloadBytes = Buffer.from(canonicalJson({
        action_type: "WorkspaceEdit",
        target_path: parameters.path,
        patch,
      }));
      const payloadDigest = sha256(payloadBytes);
      requireOutcome(await ipc.request("EnsureRegion", {
        region_ref: `region://payload/${actionId}`,
        content_digest: payloadDigest,
        content: [...payloadBytes],
        profile: "D1",
      }), "Success");
      const manifestBytes = Buffer.from(`${actionId}\n`);
      const manifestDigest = sha256(manifestBytes);
      requireOutcome(await ipc.request("EnsureRegion", {
        region_ref: "region://manifest",
        content_digest: manifestDigest,
        content: [...manifestBytes],
        profile: "D1",
      }), "Success");
      requireOutcome(await ipc.request("CommitTurn", {
        lease_epoch: state.leaseEpoch,
        base_projection_digest: state.baseDigest,
        successor_region_id: state.lastObservation.region,
        successor_digest: state.lastObservation.digest,
        action_manifest_region_id: "region://manifest",
        action_manifest_digest: manifestDigest,
        action_manifest: [actionId],
        action_bindings: [{
          action_id: actionId,
          payload_region_ref: `region://payload/${actionId}`,
          payload_digest: payloadDigest,
          actuator_id: "c04:generic",
        }],
      }), "TurnCommitted");
      const scope = `workspace:${parameters.path}`;
      requireOutcome(await ipc.request("RegisterAction", {
        action_id: actionId,
        stable_operation_id: `edit-${state.nextAction}`,
        action_family: "c04:generic",
        target_scope: scope,
      }), "ActionRegistered");
      requireOutcome(await ipc.request("PresentAdmissionCertificate", {
        action_id: actionId,
        target_scope: scope,
        capability_id: "capability-1",
        generation: 1,
      }), "AttemptArmed");
      state.baseDigest = state.lastObservation.digest;
      state.turnId += 1;
      state.active = false;
      state.lastObservation = null;
      state.terminalArmed = true;
      return {
        content: [{ type: "text", text: "Patch candidate armed for trusted host settlement and verification. Session is complete." }],
        details: { action_id: actionId },
      };
    },
  });
}
