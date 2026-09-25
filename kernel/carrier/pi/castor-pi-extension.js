import { createHash } from "node:crypto";
import { readFile, realpath } from "node:fs/promises";
import { resolve, relative, isAbsolute } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { Type, createAssistantMessageEventStream, getCurrentTools } from "@earendil-works/pi-ai";
import { AisaClient, canonicalJson } from "./protocol.js";

const EMPTY_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const WORKSPACE = "/workspace";
const sha256 = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;

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
  return readFile(physical, "utf8");
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
    active: false,
    nextInteraction: 0,
    nextAction: 0,
    lastObservation: null,
    modelBusy: false,
  };

  async function ensureTurn() {
    if (state.active) return;
    requireOutcome(await ipc.request("AdmitTurn", {
      agent_id: "agent-1",
      turn_id: state.turnId,
      lease_epoch: 0,
      base_projection_digest: state.baseDigest,
    }), "Admitted");
    state.active = true;
    state.leaseEpoch = 0;
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
    const deadline = Date.now() + 60_000;
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
      if (consumed?.type !== "RejectedCurrentState") throw new Error(`model observation rejected: ${consumed?.type}`);
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
    description: "Read a file from the immutable task snapshot.",
    parameters: Type.Object({ path: Type.String() }),
    async execute(_toolCallId, parameters) {
      const text = await readWorkspace(parameters.path);
      return { content: [{ type: "text", text }], details: { path: parameters.path } };
    },
  });

  pi.registerTool({
    name: "castor_edit_file",
    label: "Propose edit",
    description: "Submit one unified diff for governed host application; the host verifies the result independently.",
    parameters: Type.Object({ path: Type.String(), patch_diff: Type.String() }),
    async execute(_toolCallId, parameters) {
      if (!state.active || !state.lastObservation) throw new Error("model result must be bound before editing");
      if (state.nextAction > 0) throw new Error("bounded one-shot task permits one edit action");
      if (typeof parameters.path !== "string" || isAbsolute(parameters.path) || parameters.path.includes("..")) throw new Error("invalid workspace edit path");
      const actionId = `action-${++state.nextAction}`;
      const payloadBytes = Buffer.from(canonicalJson({
        action_type: "WorkspaceEdit",
        target_path: parameters.path,
        patch: parameters.patch_diff,
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
      return {
        content: [{ type: "text", text: "Edit submitted to Castor for trusted host settlement and verification." }],
        details: { action_id: actionId },
      };
    },
  });
}
