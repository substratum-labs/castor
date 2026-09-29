"""Test-only local FileBridge transport; never opens a network connection."""


def bits_reply(payload, timeout):
    del timeout
    old = "        if type(k) is int:\n            if k >= self.len:\n                raise IndexError(k)"
    new = "        if type(k) is int:\n            if k < 0:\n                k += self.len\n            if k < 0 or k >= self.len:\n                raise IndexError(k)"
    results = [m for m in payload["messages"] if m["role"] == "tool"]
    if not results:
        name, arguments = "castor_read_file", {"path": "boltons/mathutils.py"}
    elif len(results) == 1 and old in results[0]["content"]:
        name = "castor_edit_file"
        arguments = {"path": "boltons/mathutils.py", "edits": [{"oldText": old, "newText": new}]}
    else:
        raise ValueError("fake bridge requires a genuine Bits source read")
    return {
        "model": "qwen3.5:9b",
        "done": True,
        "message": {"content": "", "tool_calls": [{"id": "read-1" if not results else "edit-1", "function": {"name": name, "arguments": arguments}}]},
        "prompt_eval_count": 12,
        "eval_count": 8,
    }
