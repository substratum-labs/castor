import { createTwoFilesPatch, FILE_HEADERS_ONLY } from "diff";

// Cooperative candidate compilation only. The trusted host independently
// validates the resulting patch against its immutable task workspace.
export function compileExactEdits(path, original, edits) {
  if (typeof path !== "string" || path.length === 0 || path.includes("\n") || path.includes("\r")) {
    throw new Error("invalid edit path");
  }
  if (typeof original !== "string" || !Array.isArray(edits) || edits.length === 0) {
    throw new Error("edits must be a non-empty array");
  }
  const matched = edits.map((edit) => {
    if (typeof edit?.oldText !== "string" || edit.oldText.length === 0 || typeof edit.newText !== "string") {
      throw new Error("oldText must be non-empty and newText must be text");
    }
    const start = original.indexOf(edit.oldText);
    if (start < 0) throw new Error("oldText not found in immutable snapshot");
    if (original.indexOf(edit.oldText, start + 1) >= 0) throw new Error("oldText must be unique in immutable snapshot");
    return { start, end: start + edit.oldText.length, newText: edit.newText };
  }).sort((a, b) => a.start - b.start);
  for (let i = 1; i < matched.length; i++) {
    if (matched[i - 1].end > matched[i].start) throw new Error("edits overlap in immutable snapshot");
  }
  let changed = original;
  for (const edit of matched.reverse()) {
    changed = changed.slice(0, edit.start) + edit.newText + changed.slice(edit.end);
  }
  if (changed === original) throw new Error("edit produces no change");
  const patch = createTwoFilesPatch(`a/${path}`, `b/${path}`, original, changed, undefined, undefined, {
    context: 4,
    headerOptions: FILE_HEADERS_ONLY,
  });
  return { changed, patch };
}
