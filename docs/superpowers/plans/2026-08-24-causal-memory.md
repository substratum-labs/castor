# Causal Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Castor memory dependencies explicit, prevent unsafe eviction, and expose deterministic causal graph queries through memory syscalls.

**Architecture:** Extend `CastorMessage` with backward-compatible provenance fields and keep the directed graph in `MMU`, where existing memory lifecycle effects already execute. `SyscallProxy` applies the graph-derived eviction set after the journaled syscall returns, preserving replay behavior.

**Tech Stack:** Python 3.11, Pydantic v2, pytest/pytest-asyncio.

**Spec:** `/Users/yong/projects/substratum/substratum-internal/design/castor/briefings/castor/BRIEFING_CASTOR_CAUSAL_MEMORY.md`

## Global Constraints

- Preserve the seven existing memory syscall call shapes and replay semantics.
- Keep eviction selection as kernel mechanism, not application policy.
- Do not introduce LLM calls, storage migrations, or cross-host behavior.

---

### Task 1: Causal models and dependency recording

**Files:**
- Create: `src/castor/models/causal.py`
- Modify: `src/castor/models/checkpoint.py`, `src/castor/mmu/core.py`, `src/castor/scheduler/proxy.py`
- Test: `tests/test_causal_memory.py`

- [ ] Write a failing test proving `mem_write(depends_on=[...])` records a memory edge and preserves source trust/reason.
- [ ] Run the focused test and confirm it fails because the causal API is absent.
- [ ] Add validated provenance models and minimal graph state plus `mem_write` schema support.
- [ ] Run the focused test and confirm it passes.

### Task 2: Dependency-checked eviction

**Files:**
- Modify: `src/castor/models/causal.py`, `src/castor/mmu/core.py`, `src/castor/scheduler/proxy.py`
- Test: `tests/test_causal_memory.py`

- [ ] Write failing tests for `forbid`, `warn`, and transitive `cascade` eviction results.
- [ ] Run the focused tests and confirm they fail because eviction ignores dependents.
- [ ] Add deterministic dependent traversal and apply every approved eviction to the context/cold store.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Provenance and explanation syscalls

**Files:**
- Modify: `src/castor/mmu/core.py`, `src/castor/scheduler/proxy.py`, `src/castor/models/checkpoint.py`
- Test: `tests/test_causal_memory.py`

- [ ] Write failing tests for source/deriver graph walks, depth truncation, and structured explanation.
- [ ] Run the focused tests and confirm they fail because the syscalls are unregistered.
- [ ] Register and implement graph-query syscalls without LLM execution, tagging them as provenance.
- [ ] Run the focused tests and confirm they pass.

### Task 4: Regression verification

**Files:**
- Modify: `docs/superpowers/plans/2026-08-24-causal-memory.md`

- [ ] Run the complete Python test suite, Rust tests, formatting, and diff checks.
- [ ] Re-read the changed files and record verification evidence in the handoff.
