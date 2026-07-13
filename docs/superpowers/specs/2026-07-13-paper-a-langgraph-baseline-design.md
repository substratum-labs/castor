# Paper A LangGraph Checkpointer Baseline

## Purpose

Add a reproducible, fair LangGraph comparison to the existing S-Pay
effect-safety evaluation.  The comparison measures what a stock, persistent
LangGraph checkpointer does when a process is killed around an external
payment, without attributing Castor's operation identity or actuator-query
protocol to LangGraph.

## Scope and boundaries

- Add a pinned LangGraph runtime and its official SQLite checkpointer as a
  dedicated evaluation dependency.
- Add `b_langgraph` to the S-Pay matrix while preserving the existing result
  schema (`TrialResult`, `results.json`, and `results.md`).
- Reuse the existing S-Pay policy steps, `ActuatorBench`, effect count, and
  parent-driven SIGKILL harness.
- Exercise `kill_after_commit` and `kill_after_success` with a fresh process
  resuming the same persisted checkpoint.
- Do not modify Castor's kernel behavior or claim that LangGraph lacks
  features outside the tested configuration.

The primary baseline runs with no application-supplied tool cache or stable
external `operation_id`.  An optional cache configuration, if supported by
the pinned LangGraph API, is a separately labeled sensitivity result and is
not evidence of native end-to-end exactly-once semantics.

## Design

`b_langgraph` will be implemented in an evaluation-local worker.  It creates
the same `get_balance`, deterministic `llm_decide`, `payment`, and
`send_email` steps used by S-Pay, backed by the existing external SQLite
`ActuatorBench` with deduplication disabled.  A LangGraph `StateGraph` stores
its checkpoint through the official SQLite saver and is invoked with one
stable thread identifier for the initial and resumed processes.

The parent harness waits for the existing commit marker, sends SIGKILL, and
starts the resume process against the same checkpoint and actuator databases.
For the `kill_after_commit` fault, the payment effect has committed but the
payment node has not returned a checkpointed result.  Resumption therefore
re-enters that node; the baseline records the observed duplicate external
payment.  For `kill_after_success`, the payment node has finished and a later
agent-side suspension is used to distinguish a completed node checkpoint from
an in-flight external effect.

The matrix returns the same fields as all other systems: committed effects,
duplicate and missing effects, resume status, commit names, wall-clock time,
and error.  A small configuration metadata record identifies the exact
LangGraph and checkpointer versions, checkpoint backend, tool-cache setting,
and fault model.  The accompanying comparison note states that this evaluates
stock checkpointer recovery, not a claim about every possible LangGraph
application protocol.

## Tests and verification

Tests are added before implementation and must first fail because
`b_langgraph` is unavailable.  They assert that the baseline reaches a
completed resumed graph, reports the same two intended effects, and exposes a
duplicate payment under `kill_after_commit`.  Existing Castor S-Pay tests
remain unchanged and must stay green.  The final verification runs the focused
evaluation tests, the full test suite, Ruff checks/format validation, a
multi-trial matrix command, and reads back the generated artifacts.

## Non-goals

- No claim that a workflow graph cannot be made effect-safe with an explicit
  application-level idempotency protocol.
- No change to Castor's operation ID, journal, or recovery implementation.
- No new Paper A contribution beyond the pinned comparison.
