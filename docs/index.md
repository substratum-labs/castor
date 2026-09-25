# Castor OS

Castor OS is a single-node runtime for governed Agents. The Rust `castord` process is the authority for Agent and Turn state, capabilities, durable journal transitions, external-effect admission, and recovery. A Roche carrier isolates an untrusted Agent program; AISA sockets mediate requests between the Agent, operator, trusted actuator, evidence service, and Core.

The first supported Python interface is the thin `castor-client` package. It sends requests to `castord` and cannot create authority locally. Start with [Castor A: Rust authority with a Python Agent](castor-a.md) for the bounded D1 path, installation instructions, and explicit limits.

The earlier `castor-kernel` 0.6 Python package remains a historical in-process prototype for existing integrations. Its `Castor()` facade, tool gate, replay, MMU, budget, and `castor run` examples are not Rust D1 guarantees. Those API pages remain available in the `v0.6.0-alpha.1` Git history; they are excluded from the current product site so their stability labels do not imply Rust conformance.

Castor A does not yet provide automatic `castor run agent.py` packaging or a Python-free default Agent. Those are separate product milestones.
