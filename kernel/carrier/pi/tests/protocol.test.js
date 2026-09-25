import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import { canonicalJson, AisaClient } from "../protocol.js";

test("canonical request bytes bind all messages and tool declarations", () => {
  const left = { tools: [{ name: "castor_edit_file", parameters: { b: 2, a: 1 } }], messages: [{ role: "user", content: "fix" }], schema_version: 1 };
  const right = { schema_version: 1, messages: [{ content: "fix", role: "user" }], tools: [{ parameters: { a: 1, b: 2 }, name: "castor_edit_file" }] };
  assert.equal(canonicalJson(left), canonicalJson(right));
  assert.match(canonicalJson(left), /castor_edit_file/);
  assert.match(canonicalJson(left), /"content":"fix"/);
  assert.equal(canonicalJson({ optional: undefined, required: 1 }), '{"required":1}');
});

test("AISA client uses bounded big-endian frames over one Unix socket", async () => {
  const root = await mkdtemp(join(tmpdir(), "castor-pi-aisa-"));
  const socket = join(root, "ipc.sock");
  let observed;
  const server = createServer((stream) => {
    let data = Buffer.alloc(0);
    stream.on("data", (chunk) => {
      data = Buffer.concat([data, chunk]);
      if (data.length < 4) return;
      const length = data.readUInt32BE(0);
      if (data.length < 4 + length) return;
      observed = JSON.parse(data.subarray(4, 4 + length).toString("utf8"));
      const response = Buffer.from(JSON.stringify({ request_id: observed.request_id, status: "Ok", outcome: { type: "Success" } }));
      const header = Buffer.alloc(4);
      header.writeUInt32BE(response.length);
      stream.end(Buffer.concat([header, response]));
    });
  });
  try {
    await new Promise((resolve) => server.listen(socket, resolve));
    const result = await new AisaClient(socket).request("EnsureRegion", { region_ref: "region://model-request/interaction-1" });
    assert.equal(result.type, "Success");
    assert.equal(observed.op, "EnsureRegion");
    assert.equal(observed.payload.region_ref, "region://model-request/interaction-1");
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
