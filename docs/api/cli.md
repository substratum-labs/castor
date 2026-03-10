# CLI Reference

The `castor` CLI provides commands for running agents, inspecting checkpoints, and managing HITL decisions.

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
