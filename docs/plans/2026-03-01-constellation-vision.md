# Constellation: Distributed Orchestration for Castor Agent Runtimes

> **Status:** Vision & Exploration (pre-design)
> **Date:** 2026-03-01
> **Scope:** Problem space, architecture options, comparisons, open questions.
> Not a concrete design spec — that comes in a dedicated design session.

---

## 1. Vision & Motivation

### 1.1 The Problem

Castor is a single-node runtime that cages LLM agents inside a deterministic
execution engine with typed validation, capability budgets, context management,
and human-in-the-loop controls. It handles one agent tree per process.

Real-world agent workloads need more:

- **Hundreds of agents** running concurrently across a cluster, not just one
  agent tree on one machine.
- **Heterogeneous hardware** — some agents need GPUs (local LLM inference),
  some need high memory (large context windows), some are I/O-bound (API calls).
- **Fault tolerance** — if a node dies, the agent should resume on another node,
  not lose hours of work.
- **Cross-agent coordination** — agents on different nodes need to communicate,
  share results, and compose into workflows.
- **Cluster-wide resource governance** — global budgets for API spend, compute
  hours, storage, enforced across all agents on all nodes.
- **Elastic scaling** — spin up more agent workers when load increases, drain
  nodes for maintenance.

Castor solves none of these. By design — these are not single-node concerns.

### 1.2 The Insight

The container ecosystem solved an identical layering problem:

- **Docker** (2013): Package and run processes in isolated containers on one machine.
- **Kubernetes** (2014): Schedule and manage containers across a cluster of machines.

Docker didn't try to become Kubernetes. Kubernetes didn't try to reimplement
container isolation. The two systems compose through a narrow interface
(Container Runtime Interface / CRI), and each does its job exceptionally well.

**Constellation** is the Kubernetes to Castor's Docker.

### 1.3 The Name

Castor is a star. Stars form constellations. A Constellation is a collection of
Castor nodes working together as a distributed agent fabric. The name captures
the relationship: each node is an independent Castor runtime (a star), and the
orchestration layer connects them into a coherent system (a constellation).

### 1.4 Design Principles

1. **Castor owns the node. Constellation owns the cluster.**
   Castor handles: validation, budgets, HITL, replay, context paging — all
   within a single node. Constellation handles: placement, routing, healing,
   scaling — all across nodes. Neither layer should duplicate the other's work.

2. **The checkpoint is the portable unit.**
   Just as a container image is the unit of portability in the container world,
   `AgentCheckpoint` is the unit of portability in Constellation. Move a
   checkpoint to any node, and Castor can resume it via deterministic replay.

3. **Distribution should be invisible to agents.**
   An agent function calls `proxy.syscall("spawn_agent", ...)` and gets a
   result. It doesn't know — or care — whether the child ran on the same
   machine or across the ocean. Constellation handles routing transparently.

4. **Failure is a normal operation.**
   Nodes crash, networks partition, agents hang. Constellation must handle all
   of these as routine events, not exceptional conditions. This means:
   checkpoints must be durable, agents must be resumable, and no single node
   failure should cause permanent data loss.

5. **Decentralized enforcement, centralized governance.**
   Budget enforcement happens locally (Castor), budget allocation happens
   globally (Constellation). This mirrors how Kubernetes sets resource quotas
   at the namespace level but enforcement happens at the kubelet/cgroup level.

---

## 2. The Docker/Kubernetes Analogy (Deep Dive)

This section maps the analogy precisely to identify what Constellation must
provide, what it must NOT provide, and where the interface lives.

### 2.1 Concept Mapping

```
Container World                    Agent World
═══════════════════════            ═══════════════════════
Docker Engine                      Castor Runtime
  Container image                    AgentCheckpoint (JSON blob)
  Dockerfile                         Agent function definition
  Container process                  Agent execution (proxy + event loop)
  cgroups (CPU/mem limits)           Capabilities (budget limits)
  namespaces (isolation)             SyscallProxy (syscall boundary)
  Volume mounts                      Lodge cold storage driver
  Container lifecycle                Agent lifecycle (run/suspend/resume/preempt)
  Container logs                     Syscall log (replay journal)
  Health check                       Agent heartbeat

Kubernetes                         Constellation
  Pod                                Agent (or Agent Group?)
  Deployment/ReplicaSet              Agent Pool (scaled agent template)
  Node                               Castor Node (one Castor runtime)
  kubelet                            Castor daemon (ARI endpoint)
  kube-scheduler                     Agent Scheduler
  kube-controller-manager            Constellation Controller
  kube-apiserver                     Constellation API
  etcd                               Constellation State Store
  Service / Ingress                  Agent Endpoint / HITL Gateway
  PersistentVolume                   Distributed Lodge Storage
  Resource Quota                     Cluster Budget Pool
  Namespace                          Agent Namespace / Tenant
  Pod disruption budget              Agent disruption budget
  Horizontal Pod Autoscaler          Agent Pool Autoscaler
  CRI (Container Runtime Interface)  ARI (Agent Runtime Interface)
  CNI (Container Network Interface)  ACI (Agent Communication Interface)
  CSI (Container Storage Interface)  ASI (Agent Storage Interface)
```

### 2.2 Where the Analogy Holds

**Portable unit of execution.** A container image runs identically on any
Docker host. An `AgentCheckpoint` runs identically on any Castor node (via
deterministic replay). This portability is the foundation of both systems'
ability to schedule, migrate, and recover workloads.

**Resource isolation.** Docker uses cgroups to prevent containers from
consuming unbounded resources. Castor uses capabilities to prevent agents from
consuming unbounded API calls, tokens, or storage. Both enforce limits locally
without needing a coordinator.

**Narrow runtime interface.** Kubernetes talks to Docker through CRI — a small
set of operations (create, start, stop, status, remove). Constellation will
talk to Castor through ARI — a similarly small set (start, suspend, resume,
kill, status, heartbeat). The orchestrator doesn't need to understand agent
internals.

**Stateless workers.** In Kubernetes, worker nodes are cattle, not pets. Any
pod can run on any node (modulo affinity rules). In Constellation, any agent
can run on any Castor node — the checkpoint carries all state.

### 2.3 Where the Analogy Breaks

**Agents are stateful and long-running.** Containers are often stateless (or
externalize state to databases). Agents carry their entire execution history
in the checkpoint. Migration requires transferring this state, not just
starting a fresh instance.

**Agents spawn agents.** Containers don't spawn containers (pods do, but via
the Kubernetes API, not Docker). Agents spawn child agents as a core operation.
This creates a tree of related agents that must be co-managed — if a parent
is migrated, its in-flight children may need to follow.

**Agents have human-in-the-loop.** No container analog. HITL requests must be
routed from any node to a human decision-maker and back, potentially crossing
node boundaries when the agent that generated the request has been migrated.

**Context windows are a first-class resource.** Containers don't have an
analog to Lodge's context management. The distributed Lodge problem (evicted
memories must be accessible after migration) has no container-world parallel.

**Replay changes the failure model.** When a container crashes, you restart it
from scratch (or from a volume checkpoint). When an agent crashes, you replay
it from the syscall log — which means the agent "fast-forwards" through all
previous work without re-executing. This is much cheaper than restarting from
scratch, but it requires the checkpoint to be durable and consistent.

---

## 3. Architecture Exploration

Three possible deployment models for Constellation. Each has distinct trade-offs.

### 3.1 Model A: Centralized Server

```
┌──────────────────────────────────────────────────┐
│              Constellation Server                 │
│                                                  │
│  ┌───────────┐ ┌──────────┐ ┌────────────────┐  │
│  │ Scheduler │ │Controller│ │  State Store   │  │
│  │           │ │          │ │  (etcd/Postgres)│  │
│  └───────────┘ └──────────┘ └────────────────┘  │
│  ┌───────────┐ ┌──────────┐                     │
│  │    API    │ │   HITL   │                     │
│  │  Server   │ │  Gateway │                     │
│  └───────────┘ └──────────┘                     │
└────────┬──────────┬──────────┬───────────────────┘
         │   ARI    │          │
    ┌────┴───┐ ┌────┴───┐ ┌───┴────┐
    │ Castor │ │ Castor │ │ Castor │
    │ Node 0 │ │ Node 1 │ │ Node 2 │
    └────────┘ └────────┘ └────────┘
```

**How it works:** A dedicated Constellation server cluster manages all state.
Castor nodes are workers that pull agent checkpoints from the server, execute
them, and report results back. The server makes all scheduling decisions.

**Strengths:**
- Simplest consistency model — single source of truth
- Easiest to reason about failure modes
- Proven pattern (Temporal, Kubernetes)
- Clean separation: server = brains, workers = muscle

**Weaknesses:**
- Server is a single point of failure (needs HA)
- Server bottleneck for checkpoint I/O (every syscall persists through server?)
- Operational complexity of running the server cluster
- Latency for HITL routing through central server

**Like:** Temporal Server, Kubernetes control plane.

### 3.2 Model B: Sidecar + Lightweight Coordinator

```
┌────────────────────┐
│    Coordinator     │
│  (lightweight:     │
│   membership,      │
│   scheduling,      │
│   budget pools)    │
└──────┬─────────────┘
       │
  ┌────┼──────────┬──────────────┐
  │    │          │              │
┌─┴────┴──┐ ┌────┴─────┐ ┌─────┴────┐
│ Castor  │ │ Castor   │ │ Castor   │
│ Node 0  │ │ Node 1   │ │ Node 2   │
│┌───────┐│ │┌────────┐│ │┌────────┐│
││Sidecar││ ││Sidecar ││ ││Sidecar ││
│└───────┘│ │└────────┘│ │└────────┘│
└─────────┘ └──────────┘ └──────────┘
```

**How it works:** Each Castor node has a Constellation sidecar that handles
checkpoint replication, spawn routing, and heartbeats. A lightweight central
coordinator handles cluster membership, scheduling decisions, and global budget
pools. The coordinator is much simpler than Model A's server — it doesn't store
checkpoints or route every syscall.

**Strengths:**
- Checkpoint I/O stays local (sidecar replicates asynchronously)
- Lower latency — most operations don't hit the coordinator
- Coordinator failure is less catastrophic (nodes continue running locally)
- Simpler coordinator (no checkpoint storage, no syscall routing)

**Weaknesses:**
- More complex consistency model (eventual consistency for replicated checkpoints)
- Sidecar adds operational surface area to each node
- Split-brain scenarios if coordinator becomes unreachable
- Harder to reason about which node has the "latest" checkpoint

**Like:** Envoy sidecar pattern, Consul service mesh.

### 3.3 Model C: Peer-to-Peer

```
    ┌─────────┐
    │ Castor  │◄──────────────────────┐
    │ Node 0  │──────┐                │
    └─────────┘      │                │
         ▲           │                │
         │      ┌────┴─────┐    ┌─────┴────┐
         │      │ Castor   │    │ Castor   │
         └──────│ Node 1   │◄───│ Node 2   │
                └──────────┘    └──────────┘

    (All nodes gossip membership, checkpoints, and routing)
```

**How it works:** No central component. Castor nodes discover each other via
gossip protocol, elect leaders for scheduling decisions, and replicate
checkpoints directly between nodes. Spawn requests are routed via a distributed
hash table or consistent hashing.

**Strengths:**
- No single point of failure
- No central infrastructure to operate
- Scales horizontally with no bottleneck
- Lowest latency for node-to-node communication

**Weaknesses:**
- Most complex to implement correctly
- Consistency is hard (CAP theorem: eventual consistency or sacrificing availability)
- Debugging distributed state is painful
- Global operations (cluster budget, HITL routing) are harder without a coordinator
- Bootstrapping and membership changes are subtle

**Like:** Cassandra, Akka Cluster, CockroachDB.

### 3.4 Recommendation Direction

For a vision document, we don't need to commit. But the trade-off landscape
suggests:

**Model B (sidecar + coordinator) is likely the sweet spot** for agent workloads.
Reasons:

1. Agent checkpoints are large (KB to MB) — centralizing all I/O through a
   server (Model A) creates a bottleneck. Local persistence with async
   replication is better.

2. Agent spawning is latency-sensitive — parent blocks on `join_agent`. If
   every spawn routes through a central server, fan-out/fan-in patterns pay
   unnecessary network RTTs.

3. Global operations (budget pools, HITL routing, scheduling) genuinely need
   coordination, which P2P (Model C) makes unnecessarily hard.

4. The coordinator can be simple if it only handles cluster-level concerns
   (membership, scheduling, budget pools) and delegates node-level work to
   sidecars.

That said, **Model A is the right starting point for a prototype** — it's
simpler to build and reason about. Optimize toward Model B if latency or
scale demands it.

---

## 4. Core Concepts

### 4.1 Agent Placement

**Problem:** When an agent needs to run, which node should host it?

**Factors:**
- **Node capacity:** CPU, memory, GPU availability.
- **Affinity:** This agent needs a GPU (local LLM inference). This agent's
  Lodge cold storage is on Node 2.
- **Anti-affinity:** Don't put parent and child on the same node (fault isolation).
  Or DO put them together (latency optimization for sync spawn).
- **Budget locality:** If a cluster budget pool is managed by the coordinator,
  any node works. If budget enforcement is purely local, the agent must run
  where its budget resides (which is in the checkpoint — so any node).

**Key insight:** Because budgets travel inside the checkpoint, placement
decisions are primarily about hardware affinity and load balancing, not resource
ownership. This is simpler than Kubernetes, where pods must be matched to nodes
with the right resource capacity AND the right persistent volumes.

**Castor advantage:** Replay makes placement changes cheap. Moving an agent to a
new node just means: transfer checkpoint, replay. No state migration beyond the
checkpoint blob. In Kubernetes, migrating a stateful pod requires volume
detach/attach, which can take minutes.

### 4.2 Inter-Agent Communication

**Problem:** Parent on Node 0 calls `proxy.syscall("spawn_agent", ...)` and
the child should run on Node 1.

**Design principle:** The agent function MUST NOT know about distribution. The
syscall interface stays the same. Constellation intercepts the spawn at the
ARI boundary.

**How it could work:**

```
Node 0 (parent)                      Constellation              Node 1 (child)
     │                                     │                          │
     │ proxy.syscall("spawn_agent",        │                          │
     │   {agent: "researcher"})            │                          │
     │──── ARI: spawn_request ────────────>│                          │
     │                                     │ schedule: Node 1         │
     │                                     │──── ARI: start(cp) ─────>│
     │                                     │                          │ runs...
     │                                     │                          │ completes
     │                                     │<── ARI: completed(cp) ───│
     │<── ARI: spawn_result ──────────────│                          │
     │                                     │                          │
     │ (proxy records SyscallRecord        │                          │
     │  with child result + checkpoint)    │                          │
```

**Sync spawn:** Parent blocks until Constellation delivers the child result.
The parent's Castor runtime is suspended (preempted or waiting) during this time.

**Async spawn:** Constellation returns a handle immediately. Parent continues.
At `join_agent`, Constellation blocks until the child completes and delivers
the result.

**On replay:** The child result is cached in the parent's syscall log. The
child is NOT re-run. Constellation is not involved in replay at all — it's
purely a Castor-local operation.

### 4.3 Fault Tolerance

**Scenario 1: Child node dies.**

```
1. Node 1 (child) stops heartbeating
2. Constellation detects failure (heartbeat timeout)
3. Constellation loads child's last persisted checkpoint
4. Constellation schedules child on Node 2
5. Node 2's Castor resumes child via replay
6. Child completes on Node 2
7. Result delivered to parent on Node 0
```

From the parent's perspective, `spawn_agent` just took longer than usual.
The failure and recovery are invisible.

**Scenario 2: Parent node dies.**

```
1. Node 0 (parent) stops heartbeating
2. Constellation detects failure
3. Constellation loads parent's last persisted checkpoint
4. In-flight async children may still be running on other nodes
5. Constellation schedules parent on Node 3
6. Node 3's Castor replays parent from checkpoint
7. When parent hits spawn_agent_async syscalls: served from cache
8. When parent hits join_agent: Constellation routes to running children
   (or their completed results)
```

**Critical question:** At what granularity are checkpoints persisted?
- After every syscall? (safest, most I/O)
- After HITL suspension only? (current behavior, cheapest, most data loss risk)
- Configurable? (per-agent or per-tool durability level)

This is one of the most important design decisions for Constellation and
directly impacts the data loss window on node failure.

**Scenario 3: Network partition.**

```
Node 0 (parent) and Node 1 (child) are on opposite sides of a partition.
- Child continues running (or completes) on Node 1
- Parent is waiting for child result on Node 0
- Constellation cannot deliver the result across the partition

Options:
  a) Parent waits indefinitely (blocks until partition heals)
  b) Parent times out and gets preempted (preemption_reason: "NETWORK_PARTITION")
  c) Child result is persisted by Node 1; delivered when partition heals

Option (c) is most robust — Constellation stores child results durably,
and delivers them when connectivity is restored.
```

### 4.4 Distributed Budgets

**Two-level model:**

```
Constellation level:
  Cluster Budget Pool = { "api_usd": 10000, "gpu_hours": 500 }
  Namespace "team-alpha" quota = { "api_usd": 3000, "gpu_hours": 100 }

Castor level (within each agent):
  Root agent budget = { "api_usd": 500 } (allocated from namespace quota)
  Child agent budget = { "api_usd": 100 } (delegated from parent via Castor)
```

**Constellation manages:** Cluster-wide pools, namespace quotas, root agent
allocation. This is analogous to Kubernetes ResourceQuotas.

**Castor manages:** Parent-child delegation, per-syscall deduction, refund-on-
failure. This is analogous to cgroup limits — enforced locally, no coordinator
in the hot path.

**Budget allocation flow:**

```
1. User requests: "Run agent X with budget {api_usd: 500}"
2. Constellation checks namespace quota: 3000 available? Yes.
3. Constellation deducts 500 from namespace pool.
4. Constellation creates AgentCheckpoint with capabilities = {api_usd: 500}
5. Constellation schedules agent on a Castor node
6. Castor enforces budget locally (deduct/refund per syscall)
7. Agent completes. Castor reports final usage.
8. Constellation returns unused budget to namespace pool.
   (500 allocated - 350 used = 150 returned)
```

**Key insight:** Castor's existing `delegate()/reclaim()` mechanism works
identically at the Constellation level. The coordinator delegates from the
namespace pool to the root agent. When the agent completes, unused budget is
reclaimed. The protocol is the same — just at a higher level.

### 4.5 Distributed Lodge (Memory Layer)

**Problem:** Agent runs on Node 0, Lodge evicts context to cold storage.
Agent migrates to Node 1. How does Node 1 access the evicted memories?

**Approach: Shared cold storage backend.**

```
┌─────────┐  ┌─────────┐  ┌─────────┐
│ Castor  │  │ Castor  │  │ Castor  │
│ Node 0  │  │ Node 1  │  │ Node 2  │
└────┬────┘  └────┬────┘  └────┬────┘
     │            │            │
     └────────────┼────────────┘
                  │
     ┌────────────┴────────────┐
     │  Shared Vector Store    │
     │  (Qdrant / Pinecone /   │
     │   Weaviate cluster)     │
     └─────────────────────────┘
```

The `SemanticMemoryDriver` ABC already abstracts the storage backend. For
distribution, the driver implementation just points to a shared service instead
of a local store. No changes to Lodge core or the eviction algorithm.

**Hot context (in checkpoint) travels with the agent.** Cold context (in vector
store) is shared and accessible from any node. This is the cleanest separation:
- Checkpoint = portable, self-contained, node-local
- Cold storage = shared service, network-accessible

**Analogy:** Container image layers are cached locally for speed, but the
registry is shared for portability. Lodge's hot/cold split works the same way.

### 4.6 HITL Routing

**Problem:** Agent on Node 2 suspends for HITL. Human needs to see the request
and respond. The human is connected to a web UI or CLI that may not know which
node the agent is on.

**Solution: HITL Gateway.**

```
Human (web UI / CLI / API)
     │
     v
┌──────────────────┐
│  HITL Gateway    │ (part of Constellation)
│  (routes HITL    │
│   requests to    │
│   the right node)│
└────────┬─────────┘
         │
    ┌────┼──────────┬──────────────┐
    │    │          │              │
 Node 0  Node 1  Node 2 (agent suspended here)
```

The HITL Gateway is a Constellation service that:
1. Collects pending HITL requests from all nodes
2. Presents them to humans via API/UI
3. Routes human decisions back to the correct node
4. Handles the case where the agent has migrated since suspension

This is unique to agent orchestration — Kubernetes has no equivalent because
containers don't need human approval for operations.

### 4.7 Agent Namespaces (Multi-Tenancy)

**Problem:** Multiple teams share a Constellation cluster. Agents from
different teams shouldn't see each other's checkpoints, budgets, or memories.

**Solution: Namespaces (borrowed from Kubernetes).**

```
Constellation Cluster
  │
  ├── Namespace: team-alpha
  │     Budget pool: { api_usd: 3000 }
  │     Agents: [agent-1, agent-2, ...]
  │     Lodge storage: isolated partition
  │
  ├── Namespace: team-beta
  │     Budget pool: { api_usd: 5000 }
  │     Agents: [agent-3, agent-4, ...]
  │     Lodge storage: isolated partition
  │
  └── Namespace: team-gamma
        Budget pool: { gpu_hours: 200 }
        Agents: [agent-5, ...]
        Lodge storage: isolated partition
```

Castor doesn't need to know about namespaces. It just runs whatever checkpoint
Constellation gives it. Namespace isolation is enforced at the Constellation
level — scheduling, budget allocation, Lodge storage routing.

---

## 5. The Agent Runtime Interface (ARI)

The ARI is the contract between Constellation and Castor. It must be narrow
enough to keep the layers independent, but rich enough for Constellation to
manage agents effectively.

### 5.1 Design Principles for ARI

1. **Checkpoint-centric.** Every operation that changes agent state returns the
   updated checkpoint. Constellation never reaches inside the checkpoint — it
   treats it as an opaque blob with a few inspectable metadata fields (pid,
   status, pending_hitl).

2. **Synchronous lifecycle, asynchronous events.** Lifecycle operations (start,
   suspend, resume) are request-response. State changes (syscall completed,
   agent suspended, agent failed) are events pushed from Castor to Constellation.

3. **Minimal surface area.** If Constellation doesn't need it, it's not in ARI.
   Tool validation, budget enforcement, replay mechanics — all Castor-internal.

### 5.2 ARI Operations (Sketch)

```
Lifecycle:
  StartAgent(checkpoint) -> AgentHandle
    Load checkpoint, begin execution. Returns handle for management.

  SuspendAgent(handle) -> Checkpoint
    Preempt the agent, return serialized state.

  ResumeAgent(checkpoint) -> AgentHandle
    Load (modified) checkpoint, replay and continue.

  KillAgent(handle) -> Checkpoint
    Force-terminate, return last consistent state.

Inspection:
  GetStatus(handle) -> AgentStatus
    Returns: RUNNING, SUSPENDED_FOR_HITL, COMPLETED, FAILED, PREEMPTED

  GetCheckpoint(handle) -> Checkpoint
    Returns current serialized state without stopping the agent.

  GetMetrics(handle) -> AgentMetrics
    Syscall count, budget usage, token count, uptime, etc.

Events (Castor -> Constellation):
  OnCheckpointUpdated(pid, checkpoint)
    Fired after each syscall (or configurable batching).
    Constellation uses this for checkpoint replication.

  OnAgentSuspended(pid, checkpoint, hitl_request)
    Agent hit HITL gate. Constellation routes to HITL Gateway.

  OnAgentCompleted(pid, checkpoint, result)
    Agent finished. Constellation reclaims budget, delivers result.

  OnAgentFailed(pid, checkpoint, error)
    Agent crashed. Constellation decides: retry, reschedule, or report.

  OnSpawnRequested(parent_pid, spawn_request)
    Agent wants to spawn a child. Constellation decides placement.
    Returns: run locally (Castor handles it), or route to remote node.

Node Health:
  Ping() -> NodeHealth
    Node-level liveness check.

  ListAgents() -> list[AgentSummary]
    All agents running on this node.
```

### 5.3 The Spawn Routing Decision

The most architecturally significant ARI operation is `OnSpawnRequested`.
This is where Constellation intercepts Castor's local spawn and optionally
routes it to a remote node.

**Three modes:**

1. **Local:** Constellation says "run it here." Castor handles the spawn
   entirely in-process (current behavior). Best for low-latency, when the
   child is small, or when parent-child affinity is important.

2. **Remote:** Constellation says "run it on Node 2." Castor suspends the
   parent's spawn syscall, Constellation transfers the child checkpoint to
   Node 2, Node 2 runs the child, result flows back. Best for load balancing,
   hardware affinity, or fault isolation.

3. **Pooled:** Constellation says "run it on any node in the 'researcher' pool."
   A pool of pre-warmed Castor nodes handles a specific agent type. Best for
   fan-out patterns where many children of the same type are spawned.

The parent agent function is identical in all three cases. Only Constellation's
routing decision changes.

---

## 6. Comparison with Existing Systems

### 6.1 Temporal

**What Temporal is:** A durable execution platform for distributed workflows.
Workers execute activities (external calls) and workflows (orchestration logic).
The Temporal Server stores event histories and coordinates execution.

**Where Temporal and Constellation overlap:**

| Concern | Temporal | Constellation |
|---|---|---|
| Durable execution | Event history replay | Checkpoint/syscall log replay |
| Task routing | Task queues + workers | ARI + Castor nodes |
| Failure recovery | Server re-dispatches on timeout | Constellation reschedules |
| Child workflows | Parent-child workflow relationship | Parent-child agent spawning |
| Signaling | Workflow signals (human input) | HITL Gateway |

**Where Constellation differs fundamentally:**

1. **The execution unit is an LLM, not code.**
   Temporal assumes workflow logic is deterministic code written by programmers.
   Castor/Constellation assumes the "logic" is generated by a non-deterministic
   LLM that needs caging. This changes everything:
   - Temporal doesn't need input validation (code doesn't make type errors)
   - Temporal doesn't need budgets (code doesn't hallucinate API calls)
   - Temporal doesn't need context window management (code doesn't forget)
   - Temporal doesn't need the LLM response cached for replay

2. **HITL is a first-class primitive, not a pattern.**
   In Temporal, human approval is implemented via signals — a mechanism the
   developer builds into each workflow. In Constellation, HITL is a kernel
   feature: tools declare `destructive=True` and the system automatically
   suspends. The HITL Gateway routes requests cluster-wide. No developer effort.

3. **Budget is a kernel-enforced invariant.**
   Temporal has no resource budgeting. A workflow can call unlimited activities.
   Rate limiting is external (client-side or proxy). Castor's capability model
   ensures a child agent CANNOT exceed its parent's delegated budget — enforced
   at the proxy level with no network call. Constellation adds cluster-wide
   budget pools on top.

4. **Context management is a scheduling concern.**
   Temporal doesn't manage context windows because it doesn't run LLMs. For
   Constellation, agent placement may depend on context size (large-context
   agents need high-memory nodes), and Lodge's cold storage is a distributed
   resource that Constellation must manage.

5. **Replay semantics differ.**
   Temporal replays the full event history to rebuild workflow state. Long-
   running workflows can accumulate millions of events, requiring "continue-as-
   new" to reset. Castor's checkpoint IS the state — replay reads the syscall
   log and serves cached responses. The checkpoint is a bounded-size snapshot,
   not an unbounded event log. (Though syscall logs do grow — compaction may
   be needed for very long-running agents.)

**Could we use Temporal as Constellation's backend?**

Possibly, but with significant impedance mismatch:
- Temporal's activity model (request-response) doesn't map cleanly to Castor's
  continuous agent execution
- Temporal's event history is separate from business state; Castor's syscall
  log IS the state
- Temporal's worker model assumes stateless workers; Castor nodes may have
  Lodge cold storage locality
- Temporal doesn't understand budgets, HITL, or context windows — these would
  need to be layered on top, defeating the purpose

**Verdict:** Temporal solves a different (adjacent) problem. Constellation
should be purpose-built for LLM agent orchestration, not shoehorned onto
Temporal. However, Temporal's design provides excellent reference architecture
for task queues, failure recovery, and distributed state management.

### 6.2 Ray

**What Ray is:** A distributed compute framework for ML workloads. Ray Core
provides task and actor abstractions. Ray Serve handles model serving.

**Where Ray and Constellation overlap:**

| Concern | Ray | Constellation |
|---|---|---|
| Task distribution | Remote functions + actors | ARI + spawn routing |
| Resource management | Resource requests per task | Capability budgets |
| Fault tolerance | Actor/task checkpointing | Agent checkpoint replay |
| Scaling | Autoscaler | Agent pool autoscaler |
| Object storage | Distributed object store | Distributed Lodge storage |

**Where Constellation differs:**

1. **Ray is compute-centric; Constellation is agent-centric.**
   Ray's unit of work is a function call or actor method. It doesn't have
   opinions about what the function does. Constellation's unit of work is an
   agent — with lifecycle semantics (suspend/resume), budget constraints,
   HITL requirements, and context management.

2. **Ray has no replay/checkpoint model for agents.**
   Ray can checkpoint actors, but it doesn't have Castor's syscall log / replay
   mechanism. An agent that crashes on Ray loses all in-flight state unless
   the developer manually implements checkpointing. Castor provides this for
   free via the SyscallProxy.

3. **Ray has no HITL.**
   No concept of "this operation needs human approval." No automatic suspension,
   no structured feedback loop.

4. **Ray's distributed object store is pull-based; Lodge needs push-based eviction.**
   Ray objects are immutable and fetched on demand. Lodge evicts proactively
   based on token pressure. Different access pattern.

**Could we build Constellation on Ray?**

Ray's distributed scheduling and object store could serve as infrastructure
layers, with Castor providing the agent semantics on top. This is more viable
than Temporal because Ray is a lower-level compute fabric. But it would still
require significant work to add HITL routing, budget management, and the
ARI contract.

**Verdict:** Ray is a possible foundation layer but doesn't provide the
agent-specific abstractions Constellation needs. Building on Ray would save
work on distributed scheduling but require building everything else.

### 6.3 Kubernetes Itself

**Could Constellation literally be a Kubernetes operator?**

```
Constellation as a K8s operator:
  - CRD: CastorAgent (defines agent function, budgets, etc.)
  - Controller: Watches CastorAgent resources, manages pods
  - Each pod runs a Castor runtime
  - ConfigMaps/Secrets for agent configuration
  - PersistentVolumeClaims for Lodge cold storage
  - Service mesh for inter-agent communication
```

**Strengths:**
- Leverages existing K8s scheduling, networking, storage, health checks
- Familiar operational model for K8s-native teams
- Service mesh (Istio/Linkerd) provides inter-agent communication
- Already handles node failure, rolling updates, etc.

**Weaknesses:**
- Pod startup latency (seconds) is too slow for agent spawning (should be ms)
- K8s scheduling is optimized for long-running services, not short-lived agent
  tasks that spawn and complete in seconds
- HITL routing needs custom infrastructure regardless
- Budget management is not a K8s concept
- CRD/operator model adds operational complexity for what should be a simple
  library

**Verdict:** Running Castor nodes ON Kubernetes makes sense (just like running
Docker on K8s-managed VMs). But Constellation should not BE a Kubernetes
operator — the abstraction levels don't match. Agent spawning needs sub-second
latency that K8s pod scheduling can't provide. Constellation should be a
purpose-built system that can optionally deploy on K8s infrastructure.

### 6.4 Summary Comparison

```
                    Temporal    Ray         Kubernetes    Constellation
                    ────────    ───         ──────────    ─────────────
Target domain       workflows   ML compute  containers    LLM agents
Replay model        event log   none        none          syscall log
Budget enforcement  none        resource    resource      capabilities
                                requests    quotas        (per-syscall)
HITL support        signals     none        none          first-class
Context mgmt        N/A         N/A         N/A           Lodge
Agent spawning      child wf    remote fn   pod creation  spawn_agent
Spawn latency       ~100ms      ~10ms       ~seconds      ~1ms (local)
                                                          ~50ms (remote)
State portability   event hist  actor state pod + PV      checkpoint blob
Failure recovery    server      lineage     pod restart   checkpoint replay
```

---

## 7. Open Questions & Future Exploration

These questions need answers before Constellation can move from vision to
concrete design.

### 7.1 Checkpoint Durability

**Question:** At what granularity should checkpoints be persisted for
distributed durability?

Options:
- After every syscall (safest: max 1 syscall of data loss on node failure)
- After every N syscalls (tunable: batch persistence for throughput)
- After HITL suspension only (cheapest: but loses all in-flight work)
- Configurable per-agent (critical agents persist every syscall; batch agents
  persist less frequently)

This directly impacts I/O load, recovery guarantees, and checkpoint store
design.

### 7.2 Spawn Routing Policy

**Question:** How does Constellation decide whether to run a child locally or
remotely?

Factors: child expected duration, parent-child affinity, node load, hardware
requirements, budget constraints. Should this be declarative (annotation on
agent function) or dynamic (runtime heuristic)?

### 7.3 Agent Identity Across Migrations

**Question:** When an agent migrates from Node 0 to Node 1, does its PID
change? How do other agents address it?

Current PID format: `"{parent}::{name}-{N}"` — deterministic, stable. But
if the agent migrates, the PID must remain the same for parent-child
relationships to work. Constellation needs a PID → node mapping table.

### 7.4 Checkpoint Size Management

**Question:** For long-running agents, the syscall log (and thus checkpoint
size) grows unboundedly. How do we bound it?

Options:
- Checkpoint compaction (snapshot + truncate log, like Kafka log compaction)
- "Continue-as-new" pattern (Temporal's approach: start a new agent from the
  current state, discard old log)
- Log segmentation (keep recent log in checkpoint, archive old segments)

### 7.5 Distributed Lodge Consistency

**Question:** If an agent evicts context to cold storage, then the cold storage
node fails, what happens when the agent tries `search_memory`?

Options:
- Cold storage is replicated (like any database HA setup)
- `search_memory` returns "not found" gracefully (the LLM adapts)
- Lodge tracks which memories are in cold storage and surfaces a warning

### 7.6 HITL in a Distributed Context

**Question:** An agent suspends for HITL on Node 0. Before the human responds,
Node 0 fails. Constellation reschedules the agent on Node 1. When the human
responds, where does the response go?

The HITL Gateway must track which node is hosting the suspended agent and
redirect the response if the agent has migrated. This requires the Gateway
to be part of Constellation (not node-local).

### 7.7 Security Model for Inter-Node Communication

**Question:** How do Castor nodes authenticate with Constellation? How are
checkpoints protected in transit?

Checkpoints contain agent state including tool results — potentially sensitive
data. Inter-node communication must be encrypted and authenticated. mTLS?
Shared secrets? Token-based auth?

### 7.8 Versioning and Rolling Upgrades

**Question:** How do you upgrade Castor or agent function code across a cluster
without disrupting running agents?

Options:
- Drain nodes (suspend all agents, upgrade, resume)
- Blue-green deployment (new nodes with new version, migrate agents)
- In-place upgrade with checkpoint compatibility guarantees

Agent function code must be compatible with existing checkpoints (the syscall
log references function names). Versioning the agent function registry is
important.

### 7.9 Observability Stack

**Question:** What observability does Constellation need beyond Castor's
per-agent metrics?

Cluster-level metrics: agents per node, spawn rate, HITL queue depth, budget
utilization by namespace, checkpoint replication lag, migration frequency,
node health.

Distributed tracing: trace a parent-child agent tree across nodes, with
spans per syscall. OpenTelemetry integration.

### 7.10 Economic Model

**Question:** How do cluster-wide budgets interact with real money?

If `api_usd` maps to actual API spend, Constellation's budget pools represent
real financial commitments. Overspend due to timing (budget deducted on Node 0,
not yet replicated to coordinator, Node 1 allocates from stale pool) could
cause real financial loss. How tight does budget consistency need to be?

---

## 8. Glossary

| Term | Definition |
|---|---|
| **Castor** | Single-node agent runtime. Handles validation, budgets, HITL, replay, context paging. |
| **Constellation** | Distributed orchestration layer for Castor. Handles placement, routing, healing, scaling. |
| **ARI** | Agent Runtime Interface. The contract between Constellation and Castor (like CRI for containers). |
| **AgentCheckpoint** | The portable unit of agent state. Serializable JSON blob containing syscall log, capabilities, context, and metadata. |
| **Castor Node** | A machine running a Castor runtime, hosting one or more agents. |
| **HITL Gateway** | Constellation service that routes human-in-the-loop requests from any node to the human decision layer. |
| **Agent Pool** | A set of pre-warmed Castor nodes ready to run a specific agent type. Analogous to a Kubernetes Deployment. |
| **Namespace** | Tenant isolation boundary. Contains budget pools, agents, and Lodge storage. Analogous to a Kubernetes Namespace. |
| **Spawn Routing** | Constellation's decision of whether to run a child agent locally or on a remote node. |
| **Budget Pool** | Cluster-level resource allocation managed by Constellation. Root agents are allocated budgets from pools. |
| **Cold Storage** | Shared distributed backend for Lodge evicted memories (vector DB). Accessible from any Castor node. |
