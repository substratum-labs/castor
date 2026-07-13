# Paper A LangGraph baseline

## Pinned configuration

The Paper A `b_langgraph` row is a deliberately narrow baseline: **LangGraph 1.1.2** with its official **SQLite** saver.  It uses the stock graph checkpointer, with **tool cache: off**.  Each external actuator call receives a random, per-call ID: there is **no stable external operation_id** shared across re-execution.  The baseline does not add a Castor journal, pending-effect lookup, retry protocol, or application-side idempotency layer.

## Parity controls

The `b_langgraph` and Castor rows run the same S-Pay graph, external SQLite
actuator, effect payloads, expected completed-effect count (two), and
parent-process SIGKILL controller.  They use the same two injected crash
boundaries: `kill_after_commit` and `kill_after_success`.  Thus, the comparison
holds the workflow, actuator, payloads, expected count, kill parent, and fault
locations constant while varying the durable execution mechanism.

## Observed boundary

For this configuration, a `kill_after_commit` occurs after the payment has
reached the external actuator but before the graph has durably recorded that
node's completion.  Resuming can therefore re-enter payment; without a stable
external identity, the actuator records a duplicate payment.  At
`kill_after_success`, the durable graph checkpoint is already present, so the
resumed graph continues without a second payment.  Castor's corresponding
rows use its existing journal and effect identity protocol; the matrix records
the outcomes in the same result schema.

## Interpretation limit

This is **not a universal claim** about LangGraph applications or checkpointers.
It is an observation of the pinned stock-checkpointer configuration above.
Explicit application idempotency protocols, stable external operation IDs,
tool-result caches, custom retries, or a separate effect journal are outside
this baseline and may change the outcome.
