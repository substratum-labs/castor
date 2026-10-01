# Local Ollama adapter for the bounded Pi task

Run this adapter on the trusted host with Node.js 22 or newer:

```sh
node ollama_model_adapter.mjs /path/to/model.sock
CASTOR_MODEL_SOCKET=/path/to/model.sock castor run --task /path/to/task_manifest.json
```

The adapter connects only to `http://127.0.0.1:11434/api/chat`, pins
`qwen3.5:9b`, limits each response to 512 generated tokens, and permits three
unique interactions. It is a host process; the Pi carrier has no network
access or provider credentials. `test_adapter.mjs` exercises the adapter with
mocked fetch and never contacts Ollama.

The managed `castor run --project … --model local-ollama` entry enables strict
developer policy: `think:false`, `num_ctx:32768`, `temperature:0.2`, `seed:17`,
and `num_predict` bounded by both the caller's requested cap and 512. The larger
context is explicit so the host does not silently choose a small default;
sampling options are fixed policy, not Agent-controlled parameters. This does
not guarantee deterministic model output.

With strict policy, `done_reason:length` suppresses tool calls even if the
provider returned parseable arguments. The observation retains `stopReason:length`
and token usage; Rust classifies the digest-verified, bound observation as
`MODEL_OUTPUT_LIMIT_EXCEEDED`. The transport reservation remains `COMPLETED`
(a response was received), while the task fails. Identical-ID delivery reuses
the recorded observation; it does not initiate another inference.

The hashed observation's `provider_diagnostics` records the normalized finish
reason, UTF-8 byte counts for visible and thinking content, tool-call count and
failure code. It does not store raw thinking text. These counts diagnose future
empty output but cannot reconstruct the raw provider response from the old
T-380 trial. Historical standalone mode without developer policy is unchanged.
