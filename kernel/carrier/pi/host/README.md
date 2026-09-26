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
