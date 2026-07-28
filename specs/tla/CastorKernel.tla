--------------------------- MODULE CastorKernel ---------------------------
EXTENDS Sequences, Naturals, FiniteSets

CONSTANTS 
    EFFECTS,      \* Set of all possible side-effects
    CAPS,         \* Set of all capability tokens
    RequireCap    \* A function mapping an Effect to a Set of required CAPS

VARIABLES 
    journal, 
    capabilities, 
    agent_state, 
    cursor

vars == <<journal, capabilities, agent_state, cursor>>

\* For TLC config constant replacement
ConstRequireCap(e) == IF e = "Effect_A" THEN {"Cap_A"} ELSE {"Cap_B"}

STATES == {"RUNNING", "PENDING_HITL", "SUSPENDED"}
RECORD_TYPES == {"PROPOSED", "COMMITTED", "REJECTED"}

\* Type Invariant (Safety)
TypeOK == 
    /\ journal \in Seq(RECORD_TYPES \times EFFECTS)
    /\ capabilities \subseteq CAPS
    /\ agent_state \in STATES
    /\ cursor \in Nat

\* Initial State
Init == 
    /\ journal = <<>>
    /\ capabilities = {}
    /\ agent_state = "RUNNING"
    /\ cursor = 0

\* Transitions
GrantCapability(c) == 
    /\ capabilities' = capabilities \cup {c}
    /\ UNCHANGED <<journal, agent_state, cursor>>

RevokeCapability(c) == 
    /\ capabilities' = capabilities \ {c}
    /\ UNCHANGED <<journal, agent_state, cursor>>

Syscall_Propose(e) ==
    /\ agent_state = "RUNNING"
    /\ e \in EFFECTS
    /\ RequireCap[e] \subseteq capabilities
    /\ agent_state' = "PENDING_HITL"
    /\ journal' = Append(journal, <<"PROPOSED", e>>)
    /\ UNCHANGED <<capabilities, cursor>>

Syscall_Commit(e) ==
    /\ agent_state = "PENDING_HITL"
    /\ e \in EFFECTS
    /\ RequireCap[e] \subseteq capabilities \* TOCTOU Defense
    /\ journal' = Append(journal, <<"COMMITTED", e>>)
    /\ agent_state' = "RUNNING"
    /\ cursor' = Len(journal')
    /\ UNCHANGED <<capabilities>>

Syscall_Reject(e) ==
    /\ agent_state = "PENDING_HITL"
    /\ e \in EFFECTS
    /\ journal' = Append(journal, <<"REJECTED", e>>)
    /\ agent_state' = "RUNNING"
    /\ cursor' = Len(journal')
    /\ UNCHANGED <<capabilities>>

Fault_Preempt ==
    /\ agent_state \in {"RUNNING", "PENDING_HITL"}
    /\ agent_state' = "SUSPENDED"
    /\ UNCHANGED <<journal, capabilities, cursor>>

Resume_Execution ==
    /\ agent_state = "SUSPENDED"
    /\ agent_state' = "RUNNING"
    /\ cursor' = Len(journal)
    /\ UNCHANGED <<journal, capabilities>>

\* Next State Relation
Next == 
    \/ (\E c \in CAPS : GrantCapability(c))
    \/ (\E c \in CAPS : RevokeCapability(c))
    \/ (\E e \in EFFECTS : Syscall_Propose(e))
    \/ (\E e \in EFFECTS : Syscall_Commit(e))
    \/ (\E e \in EFFECTS : Syscall_Reject(e))
    \/ Fault_Preempt
    \/ Resume_Execution

Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

\* Liveness Property (Weak Fairness ensures it doesn't stay PENDING forever if possible to transition)
Liveness == (agent_state = "PENDING_HITL") ~> (agent_state = "RUNNING" \/ agent_state = "SUSPENDED")

=============================================================================
