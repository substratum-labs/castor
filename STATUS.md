# STATUS.md - Castor

## Current Status: ACTIVE

## Key Metadata
- **Ecosystem Context:** [castor-docs](file:///Users/yong/projects/castor-docs), [castor-server](file:///Users/yong/projects/castor-server), [castor-internal](file:///Users/yong/projects/castor-internal), [substratum-papers](file:///Users/yong/projects/substratum-papers)
- **Active Branch:** main

---

## Detailed Task Checklist
- [x] Phase 1: Python Prototype implementation (169 passing tests, 7 framework integrations)
- [x] Draft Technical Report / Whitepaper (`castor-docs/docs/whitepaper`)
- [x] Formulate theoretical microkernel pillars (Capabilities, Checkpoint/Replay, Context MMU, Preemption)
- [x] Create and compile the LaTeX Position Paper draft (`papers/castor/position-paper/`)
- [ ] Refine position paper sections based on research targets (e.g. MLSys/NeurIPS Systems)
- [ ] Implement standalone Rust daemon (`castord`)
- [ ] Formal verification of capability-based budget safety

---

## Progress Logs
### 2026-06-25 / 2026-06-26
* Formulated the four theoretical pillars: Capability-Based Security, Checkpoint/Replay Scheduling, Context Memory Virtualization (MMU), and Preemptive Interruption.
* Created a LaTeX project for the position paper under `substratum-papers/papers/castor/position-paper/` using the NeurIPS template.
* Drafted `main.tex`, `extra_pkgs.tex`, `literature.bib`, and all sections (`introduction.tex`, `architecture.tex`, `capabilities.tex`, `memory.tex`, `scheduling.tex`, `discussion.tex`, `conclusion.tex`).
* Successfully compiled the draft via `make` (`main.pdf`, 7 pages).
