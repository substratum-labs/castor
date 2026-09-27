import test from "node:test";
import assert from "node:assert/strict";
import { compileExactEdits } from "../edit-candidate.js";

const original = 'if unit == "d":\n    # wrong\n    return value * 3600\n';
const edits = [{ oldText: '    # wrong\n    return value * 3600', newText: '    return value * 86400' }];

test("exact replacement produces an applicable standard patch without a stale return", () => {
  const { changed, patch } = compileExactEdits("duration.py", original, edits);
  assert.equal(changed, 'if unit == "d":\n    return value * 86400\n');
  assert.match(patch, /^--- a\/duration\.py\n\+\+\+ b\/duration\.py\n/m);
  assert.match(patch, /^-    return value \* 3600$/m);
  assert.match(patch, /^\+    return value \* 86400$/m);
  assert.ok(patch.endsWith("\n"));
});

test("rejects nonunique, missing, empty, overlapping and no-op edits", () => {
  const duplicate = "same\nsame\n";
  assert.throws(() => compileExactEdits("a.txt", duplicate, [{ oldText: "same", newText: "x" }]), /unique|ambiguous/i);
  assert.throws(() => compileExactEdits("a.txt", original, [{ oldText: "absent", newText: "x" }]), /not found|missing/i);
  assert.throws(() => compileExactEdits("a.txt", original, [{ oldText: "", newText: "x" }]), /empty|non-empty/i);
  assert.throws(() => compileExactEdits("a.txt", original, [
    { oldText: "# wrong", newText: "x" }, { oldText: "    # wrong", newText: "y" },
  ]), /overlap/i);
  assert.throws(() => compileExactEdits("a.txt", original, [{ oldText: "# wrong", newText: "# wrong" }]), /no change/i);
  assert.throws(() => compileExactEdits("a.txt", original, [{ oldText: "    # wrong   ", newText: "x" }]), /not found|missing/i);
});

test("preserves BOM, CRLF and missing final newline outside exact edit", () => {
  const source = '\uFEFFfirst\r\nwrong\r\nlast';
  const { changed, patch } = compileExactEdits("a.txt", source, [{ oldText: "wrong", newText: "right" }]);
  assert.equal(changed, '\uFEFFfirst\r\nright\r\nlast');
  assert.match(patch, /No newline at end of file/);
});
