# JOURNAL.md - Castor

## Developer Deep Thoughts & Design Notes

### 2026-06-26: The Paradigm Shift (ActiveGraph vs. Castor)
*   **ActiveGraph's Argument:** "The Log is the Agent". They approach agent systems from an event-sourcing and reactive database angle, making auditability and reactive branching their primary features.
*   **Castor's Argument:** "The Kernel is the Guard". We approach agents from an operating systems security angle. The LLM is a stochastic, non-deterministic CPU; the agent library is user-space application code; the environment consists of memory/hardware. To protect the environment, we must intercept all intents at the system call level.
*   **Why the OS Metaphor is Stronger for Enterprise:**
    1.  *Continuous Security:* In production, you don't just want to audit what happened (ActiveGraph); you want to prevent resource exhaustion, financial overdraw, and privilege abuse *before* they happen (Castor).
    2.  *Memory Virtualization:* Context window constraints are handled logically as physical memory paging (MMU), making it a systems engineering problem rather than a prompting problem.
    3.  *Scheduling:* Durable replay allows us to pause execution (e.g. for human-in-the-loop review) and resume from the stack frame without complex coroutine serialization.

### Next Directions to Explore
*   **Wasm Sandbox for User Space:** What if user-space agent scripts run inside a WebAssembly container, and the Castor kernel is a native host process? That would enforce absolute process-level isolation in addition to application-level security.
*   **Shadow Paging in the MMU:** Speculative context pre-loading. If the agent's recent trajectory suggests it will call a specific tool or read a specific category of documents, we can fetch those blocks from the semantic memory swap space in the background.
