# Castor one-shot installation candidate

This archive installs the bounded Rust-authoritative `castor run --project` path.
It does not install the historical Python `castor-kernel` prototype or provide
`castor run agent.py`.

Unpack the archive for your host OS and CPU. Add its `bin/` directory to PATH,
or invoke `bin/castor` from the extracted directory. Keep `libexec/castor/` and
`share/castor/release.json` beside `bin/`; copying the binary alone is not an
installation. The host requires Node.js 22+, a local Docker Engine (Docker
Desktop on macOS), and local Ollama with `qwen3.5:9b`. Host Python and Cargo are
not needed. The pinned verifier image contains Python for tasks whose test
command needs it.

```sh
/path/to/extracted/bin/castor runtime prepare
/path/to/extracted/bin/castor run \
  --project /absolute/path/to/clean-git-project \
  --task-spec /absolute/path/to/task-spec.json \
  --model local-ollama
```

`runtime prepare` pulls and verifies the exact controller, Pi carrier and
verifier images in the archive's release manifest. It does not call the model.
`run` checks those local image identities again before inference and never
pulls or builds the controller from source. For each task, Castor copies the
validated project snapshot into a stopped temporary container based on the
pinned Pi carrier, saves the resulting task image, and removes the temporary
container. The project must be a clean,
committed Git tree. The task spec is a JSON file outside that tree; for example:

```json
{"schema_version":1,"task_prompt":"Fix the failing test","verification_command":["python3","-m","unittest","discover","-s","tests"]}
```

The verification command runs in the isolated verifier, not on the host.
The CLI prints one JSON envelope containing the task result, model-call count,
cleanup status and evidence directory. Exit 0 means successful task and clean
teardown; exit 1 means task failure with clean teardown; exit 2 means preflight,
launch, or cleanup uncertainty. No automatic task retry occurs. This candidate
supports only local Ollama `qwen3.5:9b`; arbitrary model providers and
`castor run agent.py` are later work.
