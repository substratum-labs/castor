import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";

test("pinned Pi CLI loads Castor provider in a networkless read-only container", { timeout: 30000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), "castor-pi-cli-"));
  const socketPath = join(root, "ipc.sock");
  const calls = [];
  const observedDigests = [];
  let modelCalls = 0;
  let projectionObservations = 0;
  const consumeAttempts = new Map();
  const server = createServer((stream) => {
    let data = Buffer.alloc(0);
    stream.on("data", (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (data.length < 4) return;
      const length = data.readUInt32BE(0);
      if (data.length < length + 4) return;
      const request = JSON.parse(data.subarray(4, 4 + length).toString("utf8"));
      calls.push(request);
      let outcome;
      switch (request.op) {
        case "ObserveProjection": {
          const ordinal = projectionObservations++;
          const projectionDigest = ordinal === 0
            ? null
            : `sha256:${createHash("sha256").update(`projection-${ordinal}`).digest("hex")}`;
          observedDigests.push(projectionDigest);
          outcome = {
            type: "ProjectionObserved",
            projection_digest: projectionDigest,
            generation: 1,
          };
          break;
        }
        case "AdmitTurn": outcome = { type: "Admitted" }; break;
        case "EnsureRegion": outcome = { type: "Success" }; break;
        case "RequestInteraction": modelCalls += 1; outcome = { type: "InteractionRequested" }; break;
        case "CommitTurn": outcome = { type: "TurnCommitted" }; break;
        case "RegisterAction": outcome = { type: "ActionRegistered" }; break;
        case "PresentAdmissionCertificate": outcome = { type: "AttemptArmed", attempt_id: 1 }; break;
        case "ConsumeInteraction": {
          const seen = consumeAttempts.get(request.payload.interaction_id) || 0;
          consumeAttempts.set(request.payload.interaction_id, seen + 1);
          if (seen === 0) {
            outcome = { type: "RejectedStaleAuthority" };
            break;
          }
          const responseBytes = Buffer.from(JSON.stringify(modelCalls === 1 ? {
            content: [{
              type: "toolCall", id: "tool-edit-1", name: "castor_edit_file",
              arguments: { path: "defect.txt", patch_diff: "--- a/defect.txt\n+++ b/defect.txt\n@@ -1 +1 @@\n-bad\n+good\n" },
            }],
            stopReason: "toolUse",
            usage: { input: 12, output: 8 },
          } : {
            content: [{ type: "text", text: "The task is complete." }],
            stopReason: "stop",
            usage: { input: 18, output: 6 },
          }));
          outcome = {
            type: "InteractionConsumed",
            payload: {
              interaction_id: request.payload.interaction_id,
              observation_region_id: `region://observation/${modelCalls}`,
              observation_digest: `sha256:${createHash("sha256").update(responseBytes).digest("hex")}`,
              content: [...responseBytes],
              lease_epoch: request.payload.lease_epoch,
            },
          };
          break;
        }
        default: throw new Error(`unexpected Pi opcode ${request.op}`);
      }
      const body = Buffer.from(JSON.stringify({ request_id: request.request_id, status: "Ok", outcome }));
      const header = Buffer.alloc(4);
      header.writeUInt32BE(body.length);
      stream.end(Buffer.concat([header, body]));
    });
  });
  try {
    await new Promise((resolve) => server.listen(socketPath, resolve));
    const child = spawn("pi", [
      "--extension", "/opt/castor/castor-pi-extension.js",
      "--no-extensions", "--no-builtin-tools", "--no-session",
      "--offline", "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-themes",
      "--mode", "json", "--print", "--model", "castor/castor-task",
      "Repair the failing unit test.",
    ], {
      cwd: "/workspace",
      env: { ...process.env, CASTOR_IPC_SOCKET: socketPath, HOME: root },
    });
    let stdout = "";
    let stderr = "";
    child.stdin.end();
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    const timeout = setTimeout(() => child.kill("SIGKILL"), 20000);
    const code = await new Promise((resolve) => child.on("exit", resolve));
    clearTimeout(timeout);
    assert.equal(code, 0, `Pi failed: ${stderr}\n${stdout}\nAISA calls: ${calls.map((call) => call.op)}`);
    assert.equal(calls.filter((call) => call.op === "RequestInteraction").length, 2, `Pi must resume after edit; calls=${calls.map((call) => call.op)}; stdout=${stdout}; stderr=${stderr}`);
    const projectionReads = calls.filter((call) => call.op === "ObserveProjection");
    const admissions = calls.filter((call) => call.op === "AdmitTurn");
    assert.equal(projectionReads.length, 2);
    assert.equal(admissions.length, 2);
    assert.deepEqual(
      admissions.map((admission) => admission.payload.base_projection_digest),
      observedDigests.map((digest) => digest ?? "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    );
    assert.deepEqual(admissions.map((admission) => admission.payload.expected_generation), [1, 1]);
    assert.ok(calls.some((call) => call.op === "CommitTurn"), "real Pi tool call must commit through Castor");
    assert.ok(calls.some((call) => call.op === "PresentAdmissionCertificate"), "real Pi tool call must arm through Castor");
    const region = calls.find((call) => call.op === "EnsureRegion");
    const request = JSON.parse(Buffer.from(region.payload.content).toString("utf8"));
    assert.deepEqual(request.tools.map((tool) => tool.name).sort(), ["castor_edit_file", "castor_read_file"]);
    assert.match(stdout, /The task is complete/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
