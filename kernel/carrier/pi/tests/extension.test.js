import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import castorExtension from "../castor-pi-extension.js";

test("Pi provider binds a full request Region before returning a buffered model result", async () => {
  const root = await mkdtemp(join(tmpdir(), "castor-pi-ext-"));
  const socketPath = join(root, "ipc.sock");
  const calls = [];
  const observedDigests = [];
  const observedGenerations = [];
  let consumeAttempts = 0;
  let observeAttempts = 0;
  let admitAttempts = 0;
  let provider;
  const tools = [];
  const fakePi = {
    registerProvider(name, config) {
      assert.equal(name, "castor");
      provider = config;
    },
    registerTool(tool) { tools.push(tool); },
  };
  const reply = (request) => {
    calls.push(request);
    const responseBody = Buffer.from(JSON.stringify({
      content: [{ type: "text", text: "The test is fixed." }],
      stopReason: "stop",
      usage: { input: 11, output: 7 },
    }));
    const observationDigest = `sha256:${createHash("sha256").update(responseBody).digest("hex")}`;
    const outcomes = {
      EnsureRegion: { type: "Success" },
      RequestInteraction: { type: "InteractionRequested" },
      CommitTurn: { type: "TurnCommitted" },
      RegisterAction: { type: "ActionRegistered" },
      PresentAdmissionCertificate: { type: "AttemptArmed", attempt_id: 1 },
      ConsumeInteraction: request.op === "ConsumeInteraction" && ++consumeAttempts === 1 ? { type: "RejectedStaleAuthority" } : {
        type: "InteractionConsumed",
        payload: {
          interaction_id: request.payload.interaction_id,
          lease_epoch: request.payload.lease_epoch,
          observation_region_id: "region://observation",
          observation_digest: observationDigest,
          content: [...responseBody],
        },
      },
    };
    let outcome = outcomes[request.op];
    if (request.op === "ObserveProjection") {
      const ordinal = observeAttempts++;
      outcome = {
        type: "ProjectionObserved",
        projection_digest: ordinal === 0 ? null : `sha256:${createHash("sha256").update(`projection-${ordinal}`).digest("hex")}`,
        generation: ordinal < 2 ? 1 : 2,
      };
      observedDigests.push(outcome.projection_digest);
      observedGenerations.push(outcome.generation);
    } else if (request.op === "AdmitTurn") {
      outcome = ++admitAttempts === 1 ? { type: "RejectedStaleAuthority" } : { type: "Admitted" };
    }
    assert.ok(outcome, `unexpected guest opcode ${request.op}`);
    return { request_id: request.request_id, status: "Ok", outcome };
  };
  const server = createServer((stream) => {
    let data = Buffer.alloc(0);
    stream.on("data", (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (data.length < 4) return;
      const length = data.readUInt32BE(0);
      if (data.length < length + 4) return;
      const request = JSON.parse(data.subarray(4, 4 + length).toString());
      const body = Buffer.from(JSON.stringify(reply(request)));
      const header = Buffer.alloc(4);
      header.writeUInt32BE(body.length);
      stream.end(Buffer.concat([header, body]));
    });
  });
  const originalSocket = process.env.CASTOR_IPC_SOCKET;
  try {
    await new Promise((resolve) => server.listen(socketPath, resolve));
    process.env.CASTOR_IPC_SOCKET = socketPath;
    castorExtension(fakePi);
    assert.deepEqual(tools.map((tool) => tool.name).sort(), ["castor_edit_file", "castor_read_file"]);
    assert.equal(typeof provider.streamSimple, "function");
    const model = { api: "castor-buffered", provider: "castor", id: "castor-task", maxTokens: 4096 };
    const context = { messages: [{ role: "user", content: "Repair the failing unit test." }] };
    const events = [];
    for await (const event of provider.streamSimple(model, context, { maxTokens: 256 })) events.push(event);
    assert.deepEqual(calls.map((call) => call.op), [
      "ObserveProjection", "AdmitTurn", "ObserveProjection", "AdmitTurn",
      "EnsureRegion", "RequestInteraction", "ConsumeInteraction", "ConsumeInteraction",
    ]);
    assert.equal(
      calls[1].payload.base_projection_digest,
      "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "an unprojected genesis state uses the documented empty digest for admission",
    );
    assert.equal(calls[1].payload.expected_generation, 1);
    assert.equal(calls[3].payload.expected_generation, 1);
    assert.equal(
      calls[3].payload.base_projection_digest,
      observedDigests[1],
      "stale admission retries with a freshly observed projection digest",
    );
    const bytes = Buffer.from(calls[4].payload.content);
    const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    assert.equal(calls[4].payload.content_digest, digest);
    assert.equal(calls[5].payload.request_digest, digest);
    const request = JSON.parse(bytes.toString());
    assert.equal(request.messages[0].content, "Repair the failing unit test.");
    assert.ok(Array.isArray(request.tools));
    assert.equal(events[0].type, "start");
    assert.equal(events.at(-1).type, "done");
    assert.equal(events.at(-1).message.content[0].text, "The test is fixed.");
    const edit = tools.find((tool) => tool.name === "castor_edit_file");
    await assert.rejects(edit.execute("bad-path", { path: "../outside", patch_diff: "patch" }), /invalid workspace edit path/);
    await edit.execute("edit-1", { path: "defect.txt", patch_diff: "--- a/defect.txt\n+++ b/defect.txt\n@@ -1 +1 @@\n-bad\n+good\n" });
    assert.deepEqual(calls.slice(8).map((call) => call.op), [
      "EnsureRegion", "EnsureRegion", "CommitTurn", "RegisterAction", "PresentAdmissionCertificate",
    ]);
    const committed = calls.find((call) => call.op === "CommitTurn").payload;
    assert.equal(committed.action_bindings[0].payload_digest, calls[8].payload.content_digest);
    assert.equal(committed.action_manifest_digest, calls[9].payload.content_digest);
    const fencedEvents = [];
    for await (const event of provider.streamSimple(model, context, { maxTokens: 256 })) fencedEvents.push(event);
    assert.equal(fencedEvents.at(-1).type, "error");
    assert.match(fencedEvents.at(-1).error.errorMessage, /projection generation changed; task is fenced/);
    assert.equal(calls.filter((call) => call.op === "AdmitTurn").length, 2);
    assert.equal(calls.at(-1).op, "ObserveProjection");
    assert.deepEqual(observedGenerations, [1, 1, 2]);
  } finally {
    if (originalSocket === undefined) delete process.env.CASTOR_IPC_SOCKET;
    else process.env.CASTOR_IPC_SOCKET = originalSocket;
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
