// Offline probe; run in the pinned Pi carrier with this repository mounted at /src.
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { createHash } from "node:crypto";
import { getSystemMessageText } from "/opt/castor/node_modules/@earendil-works/pi-ai/dist/utils/text.js";
import { applyEditsToNormalizedContent, generateUnifiedPatch } from "/opt/castor/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/edit-diff.js";
import { formatMessagesForOllama } from "/src/kernel/carrier/pi/host/ollama_model_adapter.mjs";

const input = (name) => readFileSync(`/src/experiments/t337h/inputs/${name}`, "utf8");
const source = readFileSync("/src/kernel/carrier/pi/castor-pi-extension.js", "utf8");
const lintSource = source.slice(source.indexOf("function checkPatchShape("), source.indexOf("function requireOutcome("));
const lint = runInNewContext(`(${lintSource.trim()})`);
const raw = input("model.patch");
const original = input("duration.txt");
const cases = {
  raw,
  newline_only: raw + "\n",
  counts_only: raw.replace("@@ -37,7 +37,7 @@", "@@ -37,6 +37,6 @@"),
  counts_and_newline: raw.replace("@@ -37,7 +37,7 @@", "@@ -37,6 +37,6 @@") + "\n",
};
const comment = "        # Defect: incorrectly using 3600 instead of 86400 (24 * 3600)";
const probes = {
  // Investigator-translated, preserving the observed model's actual replacement.
  same_intent: [{ oldText: comment, newText: "        return value * 86400" }],
  // Investigator-written ideal edit. This is NOT new model output.
  clean_edit: [{ oldText: comment + "\n        return value * 3600", newText: "        return value * 86400" }],
  ambiguous: [{ oldText: "        return value * 3600", newText: "        return value * 86400" }],
  missing: [{ oldText: "not present", newText: "x" }],
  empty: [{ oldText: "", newText: "x" }],
  overlapping: [{ oldText: comment, newText: "x" }, { oldText: comment + "\n        return value * 3600", newText: "y" }],
  // Explicitly expose upstream fuzzy semantics; a strict Castor proposal must not inherit this.
  fuzzy_whitespace: [{ oldText: comment + "   ", newText: "        return value * 86400" }],
};
const native = {};
for (const [name, edits] of Object.entries(probes)) {
  try {
    const { baseContent, newContent } = applyEditsToNormalizedContent(original, edits, "duration.py");
    const upstreamPatch = generateUnifiedPatch("duration.py", baseContent, newContent);
    // Only adapt file headers to the frozen WorkspaceEdit diff contract.
    const patch = upstreamPatch.replace(/^--- duration\.py/m, "--- a/duration.py").replace(/^\+\+\+ duration\.py/m, "+++ b/duration.py");
    native[name] = { accepted: true, patch, output_sha256: createHash("sha256").update(newContent).digest("hex") };
    if (["same_intent", "clean_edit"].includes(name)) cases[`native_${name}`] = patch;
  } catch (error) { native[name] = { accepted: false, error: error.message }; }
}
// Experimental strict compiler: original-file matching, no fuzzy normalization.
// Intentionally scoped to this one fixture; not a production tool/path API.
function strictReplace(content, edits) {
  assert.ok(Array.isArray(edits) && edits.length > 0);
  const matches = edits.map(({ oldText, newText }) => {
    assert.ok(typeof oldText === "string" && oldText.length > 0);
    assert.equal(typeof newText, "string");
    const start = content.indexOf(oldText);
    assert.ok(start >= 0, "oldText missing");
    assert.equal(content.indexOf(oldText, start + 1), -1, "oldText ambiguous");
    return { start, end: start + oldText.length, newText };
  }).sort((a, b) => a.start - b.start);
  for (let i = 1; i < matches.length; i++) assert.ok(matches[i - 1].end <= matches[i].start, "overlapping edits");
  let result = content;
  for (const match of matches.reverse()) result = result.slice(0, match.start) + match.newText + result.slice(match.end);
  assert.notEqual(result, content, "no change");
  return result;
}
const strict = {};
for (const [name, edits] of Object.entries(probes)) {
  try { strictReplace(original, edits); strict[name] = "accepted"; }
  catch { strict[name] = "rejected"; }
}
for (const name of ["ambiguous", "missing", "empty", "overlapping", "fuzzy_whitespace"]) assert.equal(strict[name], "rejected");
const livePath = "/src/experiments/t337h/live-results.json";
if (existsSync(livePath)) {
  const live = JSON.parse(readFileSync(livePath, "utf8"));
  assert.equal(live.calls.length, 2);
  for (const call of live.calls) {
    const blocks = call.converted.content.filter((block) => block.type === "toolCall");
    assert.equal(blocks.length, 1);
    const tool = blocks[0];
    assert.equal(tool.name, "castor_edit_file");
    assert.equal(tool.arguments.path, "duration.py");
    if (call.mode === "raw_diff") {
      cases.live_raw = tool.arguments.patch_diff;
      // Diagnostic only: shows that format repair does not fix incorrect context.
      cases.live_raw_format_repaired = cases.live_raw.replace("@@ -35,7 +35,7 @@", "@@ -35,6 +35,6 @@") + "\n";
    } else {
      const edited = strictReplace(original, tool.arguments.edits);
      cases.live_structured = generateUnifiedPatch("duration.py", original, edited)
        .replace(/^--- duration\.py/m, "--- a/duration.py").replace(/^\+\+\+ duration\.py/m, "+++ b/duration.py");
    }
  }
}
const patches = {};
for (const [name, patch] of Object.entries(cases)) {
  try { lint("duration.py", patch); patches[name] = { patch, lint: "accepted" }; }
  catch (error) { patches[name] = { patch, lint: error.message }; }
}
const request = JSON.parse(input("request.json"));
const system = request.messages.find((message) => message.role === "system");
const actual = formatMessagesForOllama(request.messages).find((message) => message.role === "system").content;
const expected = getSystemMessageText(system);
assert.equal(actual, "");
assert.ok(expected.includes("expert coding assistant"));
assert.equal(patches.raw.lint, "patch hunk line count mismatch");
assert.equal(patches.counts_only.lint, "accepted");
for (const name of ["same_intent", "clean_edit", "fuzzy_whitespace"]) assert.equal(native[name].accepted, true);
for (const name of ["ambiguous", "missing", "empty", "overlapping"]) assert.equal(native[name].accepted, false);
console.log(JSON.stringify({
  kind: "offline_diagnostic_not_live_model", pi_version: "0.87.1",
  system_prompt: { actual_length: actual.length, upstream_rendered_length: expected.length, sections: Object.keys(system.sections), expected },
  patches, native, strict,
}, null, 2));
