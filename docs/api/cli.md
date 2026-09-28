# CLI Reference

## Rust project pack and run

The Rust `castor` binary packages an existing, clean Git project and a JSON task specification kept outside that project. The project must have a committed `HEAD`, a matching index and worktree, and no untracked files. Symlinks, special files, sparse or partial repositories, and unsafe Git metadata are rejected. Pack runs on supported Unix hosts; `run --project` requires Linux because the host model Unix socket must reach the Pi carrier.

```json
{
  "schema_version": 1,
  "task_prompt": "Fix the failing fixture",
  "verification_command": ["sh", "-c", "test \"$(cat defect.txt)\" = \"fixed fixture\""],
  "verification_timeout_seconds": 300,
  "verification_assets": "assets",
  "exclude_paths": ["notes/private.txt"]
}
```

`verification_assets` is an optional directory relative to the task-spec file. Its regular, nonexecutable files enter the snapshot under `.castor_verification_assets/`. `exclude_paths` lists exact relative files or directory prefixes ending in `/`; built-in secret and build-output exclusions also apply. Excluded paths appear in the receipt. Non-null `limits` are unsupported. `task_id` and `idempotency_key` may be supplied together; otherwise both are derived from the commit, snapshot, canonical spec, and image pins.

```bash
castor pack --project /work/my-project --task-spec /work/task.json --out /work/bundle
castor run --project /work/my-project --task-spec /work/task.json
```

`pack` publishes `manifest.json`, `workspace.tar`, and `pack-receipt.json` as one bundle, prints the receipt JSON, and reuses an existing output only when all three files match exactly. `run --project` packages into `CASTOR_STATE_ROOT/pack` (or `$HOME/.castor/state/pack`), then runs the normal one-shot task. It needs a reachable host model service at `CASTOR_MODEL_SOCKET` and locally installed Docker images: `substratum/castor-pi-carrier:v1` and the pinned `python:3.12-slim` verifier image. Build the carrier with `docker build -t substratum/castor-pi-carrier:v1 <CASTOR_DIR>/kernel/carrier/pi`. The product host does not need Python; the verifier image contains Python for the target test profile.

On successful execution, `run` prints one JSON task result with `status: "SUCCEEDED"`, `test_passed: true`, `patch_diff`, `settled_actions_count`, and `verifier_evidence`. A failed task prints a `FAILED` result and exits nonzero. Repeating the same project and spec reuses the task identity and replays its settled result without another model call.

## Legacy Python CLI

> **Legacy Python prototype CLI.** These commands use the in-process `Castor()` facade and do not run through Rust `castord`. See [Castor A usage and limits](../castor-a.md) for the current bounded D1 path. Automatic `castor run agent.py` packaging remains future work.

The historical `castor` CLI provides commands for running agents, inspecting checkpoints, and managing HITL decisions.

## Usage

```bash
castor <command> [options]
```

## Commands

### `castor run`

Run an agent function from a Python file.

```bash
castor run <agent_spec> [--budget KEY=VALUE ...] [--pid PID] [--store PATH] [--hitl interactive]
```

**Agent spec format:** `path/to/file.py:function_name`

If the function name is omitted, Castor looks for a function named `agent` or `main` in the file.

**Options:**

| Option | Description |
|--------|-------------|
| `--budget KEY=VALUE` | Set budget (repeatable). Example: `--budget api=100 --budget disk=20` |
| `--pid PID` | Set the process ID for the checkpoint |
| `--store PATH` | SQLite database path for persistence |
| `--hitl interactive` | Enable interactive HITL approval in the terminal |

**Examples:**

```bash
castor run examples/quickstart.py:research_agent --budget api=10
castor run my_agent.py --budget api=100 --budget disk=20 --hitl interactive
```

### `castor ps`

List all agent checkpoints.

```bash
castor ps --store PATH
```

Shows PID, status, and agent name for all checkpoints in the store. Status markers: `[HITL]`, `[DONE]`, `[RUN]`, `[PREM]`, `[FAIL]`.

### `castor inspect`

Show detailed information about a checkpoint.

```bash
castor inspect <pid> --store PATH
```

Displays: PID, status, agent name, capabilities, syscall log, pending HITL request, and result.

### `castor reject`

Reject a pending HITL request.

```bash
castor reject <pid> --reason "..." --store PATH
```

### `castor modify`

Modify a pending HITL request with feedback.

```bash
castor modify <pid> --feedback "..." --store PATH
```

## Module Reference

::: castor.cli.run.load_agent_function

::: castor.cli.run.parse_budgets

::: castor.cli.process.cmd_ps

::: castor.cli.process.cmd_inspect

::: castor.cli.hitl.cmd_reject

::: castor.cli.hitl.cmd_modify
