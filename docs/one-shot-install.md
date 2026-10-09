# Install the public Castor one-shot RC2 prerelease

The public [one-shot-v0.1.0-rc2 release](https://github.com/substratum-labs/castor/releases/tag/one-shot-v0.1.0-rc2)
installs the bounded Rust-authoritative `castor run --project` path. It is a
prerelease, not a general Agent runtime. It does not install the historical
Python `castor-kernel` prototype or provide `castor run agent.py`.

Choose the archive matching the **host** OS and CPU. Docker Engine is a separate
Linux environment on macOS; `runtime prepare` selects the image variant for the
Engine automatically.

| Host | Archive |
| --- | --- |
| macOS Apple Silicon | [darwin-arm64](https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2/castor-one-shot-0.1.0-rc2-darwin-arm64.tar.gz) |
| macOS Intel | [darwin-amd64](https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2/castor-one-shot-0.1.0-rc2-darwin-amd64.tar.gz) |
| Linux x86-64 | [linux-amd64](https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2/castor-one-shot-0.1.0-rc2-linux-amd64.tar.gz) |
| Linux ARM64 | [linux-arm64](https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2/castor-one-shot-0.1.0-rc2-linux-arm64.tar.gz) |

Download [SHA256SUMS](https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2/SHA256SUMS)
beside the archive and verify the selected entry before unpacking. For example,
on Apple Silicon:

```sh
asset=castor-one-shot-0.1.0-rc2-darwin-arm64.tar.gz
base=https://github.com/substratum-labs/castor/releases/download/one-shot-v0.1.0-rc2
curl -fL -O "$base/SHA256SUMS"
curl -fL -O "$base/$asset"
grep -F "  $asset" SHA256SUMS | shasum -a 256 -c -
```

For Linux, use `sha256sum -c -` in the checksum line. If you have GitHub CLI,
you can also verify source-bound provenance:

```sh
gh attestation verify "$asset" \
  --repo substratum-labs/castor \
  --signer-workflow substratum-labs/castor/.github/workflows/one-shot-release.yml \
  --source-ref refs/tags/one-shot-v0.1.0-rc2 \
  --source-digest c06e1190268df23e560acded84af968f419f550a
```

Unpack the verified archive:

```sh
mkdir castor-rc2
tar -xzf "$asset" -C castor-rc2
```

Add the extracted `bin/` directory to PATH, or invoke its `bin/castor` directly.
Keep `libexec/castor/` and
`share/castor/release.json` beside `bin/`; copying the binary alone is not an
installation. The host requires Node.js 22+, a local Docker Engine (Docker
Desktop on macOS), and local Ollama with `qwen3.5:9b`. Host Python and Cargo are
not needed. The pinned verifier image contains Python for tasks whose test
command needs it. Confirm `node --version`, `docker info`, and `ollama list`
before running. If the model is absent, `ollama pull qwen3.5:9b` downloads it;
model download and task inference are separate from image preparation.

```sh
castor-rc2/bin/castor runtime prepare
castor-rc2/bin/castor run \
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

The verification command runs in the isolated verifier, not on the host; any
test dependencies must exist in that pinned verifier image. The run may make
real local Ollama calls, so use a task you intend to execute. Castor does not
apply the candidate patch to the input Git project; inspect the result and
patch before using it.
The CLI prints one JSON envelope containing the task result, model-call count,
cleanup status and evidence directory. Exit 0 means successful task and clean
teardown; exit 1 means task failure with clean teardown; exit 2 means preflight,
launch, or cleanup uncertainty. No automatic task retry occurs. This prerelease
supports only local Ollama `qwen3.5:9b`; arbitrary model providers and
`castor run agent.py` are later work.
