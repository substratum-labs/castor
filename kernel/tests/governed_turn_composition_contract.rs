//! Hostile C-06 composition checks over one D1 durable-storage root.
//!
//! C-06 is currently test-only. This harness composes persisted C-01 lease,
//! commit, and fence records without introducing a production boundary.

use castor_kernel::c01_storage::{
    AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use sha2::{Digest, Sha256};
use tempfile::TempDir;

const AGENT_ID: &str = "agent-governed-turn";
const INCARNATION_ID: &str = "incarnation:governed-turn";
const TURN_ID: u64 = 41;
const CORE_EPOCH: u64 = 3;
const GENERATION: u64 = 9;
const LEASE_EPOCH: u64 = 17;
const BASE: &[u8] = b"base projection v1";
const INPUT: &[u8] = b"user: transfer 42 credits";
const OUTPUT: &[u8] = b"assistant: transfer requires admission";
const INTERACTION: &[u8] = b"interaction lineage: none";
const SETTLEMENT: &[u8] = b"settlement lineage: none";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

#[derive(Clone)]
struct Authority {
    agent_id: String,
    core_epoch: u64,
    generation: u64,
    incarnation: String,
    turn_id: u64,
    lease_epoch: u64,
    base: String,
}
struct Start {
    authority: Authority,
    input_region: String,
    input_digest: String,
    policy_digest: String,
    entry_id: u64,
}
struct Completion {
    authority: Authority,
    output_region: String,
    output_digest: String,
    interaction_digest: String,
    settlement_digest: String,
    successor: String,
    entry_id: u64,
}

#[derive(Debug, PartialEq, Eq)]
enum Outcome {
    Started(u64),
    Committed(u64, String),
    SameCompletion(u64),
    Quarantined(u64),
    Aborted(u64),
    Stale {
        generation: u64,
        lease_epoch: Option<u64>,
    },
    CurrentState,
    Precondition,
    Integrity,
}

#[derive(Clone)]
struct Live {
    authority: Authority,
    entry_id: u64,
}
#[derive(Clone, PartialEq, Eq)]
struct CompletionData {
    output: String,
    interaction: String,
    settlement: String,
    successor: String,
}

/// Test-only C-06 composition over a single D1 root.
struct GovernedTurnHarness {
    root: TempDir,
    storage: Option<D1DurableStorage>,
    generation: u64,
    base: String,
    live: Option<Live>,
    committed: Option<(CompletionData, u64)>,
}

impl GovernedTurnHarness {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("temporary D1 root");
        let mut storage = D1DurableStorage::open(root.path()).expect("open sole D1 storage");
        for (id, bytes) in [
            ("region://input/41", INPUT),
            ("region://interaction/41", INTERACTION),
            ("region://settlement/41", SETTLEMENT),
        ] {
            assert!(matches!(
                storage.ensure_region(id, &digest(bytes), bytes, DurabilityProfile::D1),
                EnsureRegionOutcome::Success(_)
            ));
        }
        Self {
            root,
            storage: Some(storage),
            generation: GENERATION,
            base: digest(BASE),
            live: None,
            committed: None,
        }
    }

    fn storage(&mut self) -> &mut D1DurableStorage {
        self.storage.as_mut().expect("storage open")
    }
    fn stale(&self) -> Outcome {
        Outcome::Stale {
            generation: self.generation,
            lease_epoch: self.live.as_ref().map(|live| live.authority.lease_epoch),
        }
    }
    fn current(&self, authority: &Authority) -> bool {
        authority.agent_id == AGENT_ID
            && authority.core_epoch == CORE_EPOCH
            && authority.generation == self.generation
            && authority.incarnation == INCARNATION_ID
    }
    fn append(&mut self, request: AppendConditionalRequest) -> Result<u64, Outcome> {
        match self.storage().append_conditional(request) {
            AppendConditionalOutcome::EntryPersisted(proof)
            | AppendConditionalOutcome::AlreadyPersistedSameEntry(proof) => Ok(proof.entry_id),
            AppendConditionalOutcome::RejectedPrecondition { .. } => Err(Outcome::Precondition),
            AppendConditionalOutcome::RejectedMissingOrUnpersistedRegion
            | AppendConditionalOutcome::UnavailableBeforeAck
            | AppendConditionalOutcome::IntegrityFault => Err(Outcome::Integrity),
        }
    }
    fn start(&mut self, request: Start) -> Outcome {
        if !self.current(&request.authority) {
            return self.stale();
        }
        if request.authority.base != self.base {
            return Outcome::Precondition;
        }
        if request.input_region != "region://input/41"
            || request.input_digest != digest(INPUT)
            || request.policy_digest != digest(b"policy: governed-turn-v1")
        {
            return Outcome::Integrity;
        }
        if let Some(live) = &self.live {
            return if live.authority.turn_id == request.authority.turn_id
                && live.authority.lease_epoch == request.authority.lease_epoch
                && live.entry_id == request.entry_id
            {
                Outcome::Started(request.entry_id)
            } else {
                Outcome::CurrentState
            };
        }
        let append = AppendConditionalRequest {
            agent_id: request.authority.agent_id.clone(),
            entry_id: request.entry_id,
            expected_core_epoch: request.authority.core_epoch,
            expected_agent_generation: Some(request.authority.generation),
            expected_turn_id: Some(request.authority.turn_id),
            expected_lease_epoch: Some(request.authority.lease_epoch),
            expected_base_projection_digest: Some(request.authority.base.clone()),
            entry: CoreEntry::LeaseGranted {
                turn_id: request.authority.turn_id,
                lease_epoch: request.authority.lease_epoch,
            },
            region_refs: vec![request.input_region],
        };
        match self.append(append) {
            Ok(entry_id) => {
                self.live = Some(Live {
                    authority: request.authority,
                    entry_id,
                });
                Outcome::Started(entry_id)
            }
            Err(outcome) => outcome,
        }
    }
    fn complete(&mut self, report: Completion) -> Outcome {
        if !self.current(&report.authority) {
            return self.stale();
        }
        let data = CompletionData {
            output: report.output_digest.clone(),
            interaction: report.interaction_digest.clone(),
            settlement: report.settlement_digest.clone(),
            successor: report.successor.clone(),
        };
        if let Some((committed, entry_id)) = &self.committed {
            return if committed == &data {
                Outcome::SameCompletion(*entry_id)
            } else {
                Outcome::Quarantined(report.entry_id)
            };
        }
        let Some(live) = &self.live else {
            return Outcome::CurrentState;
        };
        if live.authority.turn_id != report.authority.turn_id
            || live.authority.lease_epoch != report.authority.lease_epoch
            || live.authority.base != report.authority.base
        {
            return self.stale();
        }
        if report.output_region != "region://output/41"
            || report.output_digest != digest(OUTPUT)
            || report.interaction_digest != digest(INTERACTION)
            || report.settlement_digest != digest(SETTLEMENT)
            || report.successor.is_empty()
        {
            return Outcome::Integrity;
        }
        assert!(matches!(
            self.storage().ensure_region(
                &report.output_region,
                &report.output_digest,
                OUTPUT,
                DurabilityProfile::D1
            ),
            EnsureRegionOutcome::Success(_) | EnsureRegionOutcome::AlreadyPersistedSameContent(_)
        ));
        let append = AppendConditionalRequest {
            agent_id: report.authority.agent_id.clone(),
            entry_id: report.entry_id,
            expected_core_epoch: report.authority.core_epoch,
            expected_agent_generation: Some(report.authority.generation),
            expected_turn_id: Some(report.authority.turn_id),
            expected_lease_epoch: Some(report.authority.lease_epoch),
            expected_base_projection_digest: Some(report.authority.base.clone()),
            entry: CoreEntry::TurnCommitted {
                turn_id: report.authority.turn_id,
                successor_projection_digest: Some(report.successor.clone()),
                action_manifest_digest: None,
            },
            region_refs: vec![
                report.output_region,
                "region://interaction/41".into(),
                "region://settlement/41".into(),
            ],
        };
        match self.append(append) {
            Ok(entry_id) => {
                self.base = report.successor.clone();
                self.live = None;
                self.committed = Some((data, entry_id));
                Outcome::Committed(entry_id, report.successor)
            }
            Err(outcome) => outcome,
        }
    }
    fn abort(&mut self, authority: Authority, entry_id: u64) -> Outcome {
        if !self.current(&authority) {
            return self.stale();
        }
        let Some(live) = &self.live else {
            return Outcome::CurrentState;
        };
        if live.authority.turn_id != authority.turn_id
            || live.authority.lease_epoch != authority.lease_epoch
        {
            return self.stale();
        }
        let next = self.generation + 1;
        let append = AppendConditionalRequest {
            agent_id: authority.agent_id,
            entry_id,
            expected_core_epoch: authority.core_epoch,
            expected_agent_generation: Some(self.generation),
            expected_turn_id: Some(authority.turn_id),
            expected_lease_epoch: Some(authority.lease_epoch),
            expected_base_projection_digest: Some(self.base.clone()),
            entry: CoreEntry::FenceRevoked { generation: next },
            region_refs: vec![],
        };
        match self.append(append) {
            Ok(entry_id) => {
                self.generation = next;
                self.live = None;
                Outcome::Aborted(entry_id)
            }
            Err(outcome) => outcome,
        }
    }
    fn fence(&mut self, entry_id: u64) {
        assert!(matches!(
            self.abort(authority(), entry_id),
            Outcome::Aborted(_)
        ));
    }
    fn reconstruct(&mut self) {
        let root = self.root.path().to_path_buf();
        drop(self.storage.take());
        self.storage = Some(D1DurableStorage::open(root).expect("reopen sole D1 root"));
        let live_entry_id = self.live.as_ref().map(|live| live.entry_id);
        if let Some(entry_id) = live_entry_id {
            assert!(self.storage().read_entry(AGENT_ID, entry_id).is_some());
        }
    }
    fn active_lease(&self) -> Option<u64> {
        self.live.as_ref().map(|live| live.authority.lease_epoch)
    }
    fn unresolved(&self, turn_id: u64) -> bool {
        self.live
            .as_ref()
            .is_some_and(|live| live.authority.turn_id == turn_id)
    }
}

fn authority() -> Authority {
    Authority {
        agent_id: AGENT_ID.into(),
        core_epoch: CORE_EPOCH,
        generation: GENERATION,
        incarnation: INCARNATION_ID.into(),
        turn_id: TURN_ID,
        lease_epoch: LEASE_EPOCH,
        base: digest(BASE),
    }
}
fn start(entry_id: u64) -> Start {
    Start {
        authority: authority(),
        input_region: "region://input/41".into(),
        input_digest: digest(INPUT),
        policy_digest: digest(b"policy: governed-turn-v1"),
        entry_id,
    }
}
fn completion(entry_id: u64) -> Completion {
    Completion {
        authority: authority(),
        output_region: "region://output/41".into(),
        output_digest: digest(OUTPUT),
        interaction_digest: digest(INTERACTION),
        settlement_digest: digest(SETTLEMENT),
        successor: digest(b"base projection v2"),
        entry_id,
    }
}
fn start_turn(core: &mut GovernedTurnHarness, entry_id: u64) {
    assert_eq!(core.start(start(entry_id)), Outcome::Started(entry_id));
}

#[test]
fn test_comp_normal_governed_turn_commits_successor() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 10);
    assert_eq!(
        c.complete(completion(11)),
        Outcome::Committed(11, digest(b"base projection v2"))
    );
}
#[test]
fn test_comp_start_rejects_wrong_agent() {
    let mut c = GovernedTurnHarness::new();
    let mut r = start(20);
    r.authority.agent_id = "other".into();
    assert_eq!(
        c.start(r),
        Outcome::Stale {
            generation: GENERATION,
            lease_epoch: None
        }
    );
}
#[test]
fn test_comp_start_rejects_wrong_epoch() {
    let mut c = GovernedTurnHarness::new();
    let mut r = start(30);
    r.authority.core_epoch += 1;
    assert_eq!(
        c.start(r),
        Outcome::Stale {
            generation: GENERATION,
            lease_epoch: None
        }
    );
}
#[test]
fn test_comp_start_rejects_wrong_incarnation() {
    let mut c = GovernedTurnHarness::new();
    let mut r = start(40);
    r.authority.incarnation = "stale".into();
    assert_eq!(
        c.start(r),
        Outcome::Stale {
            generation: GENERATION,
            lease_epoch: None
        }
    );
}
#[test]
fn test_comp_start_rejects_stale_projection() {
    let mut c = GovernedTurnHarness::new();
    let mut r = start(50);
    r.authority.base = digest(b"stale");
    assert_eq!(c.start(r), Outcome::Precondition);
}
#[test]
fn test_comp_lost_start_ack_recovers_persisted_turn() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 60);
    assert_eq!(c.start(start(60)), Outcome::Started(60));
}
#[test]
fn test_comp_rejects_second_live_turn() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 70);
    let mut r = start(71);
    r.authority.turn_id += 1;
    assert_eq!(c.start(r), Outcome::CurrentState);
}
#[test]
fn test_comp_completion_requires_active_lease() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 80);
    let mut r = completion(81);
    r.authority.lease_epoch += 1;
    assert_eq!(
        c.complete(r),
        Outcome::Stale {
            generation: GENERATION,
            lease_epoch: Some(LEASE_EPOCH)
        }
    );
}
#[test]
fn test_comp_completion_rejects_wrong_output_digest() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 100);
    let mut r = completion(101);
    r.output_digest = digest(b"other");
    assert_eq!(c.complete(r), Outcome::Integrity);
}
#[test]
fn test_comp_completion_rejects_unknown_interaction_lineage() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 110);
    let mut r = completion(111);
    r.interaction_digest = digest(b"unknown");
    assert_eq!(c.complete(r), Outcome::Integrity);
}
#[test]
fn test_comp_completion_rejects_unknown_settlement_lineage() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 120);
    let mut r = completion(121);
    r.settlement_digest = digest(b"unknown");
    assert_eq!(c.complete(r), Outcome::Integrity);
}
#[test]
fn test_comp_completion_requires_successor_digest() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 130);
    let mut r = completion(131);
    r.successor.clear();
    assert_eq!(c.complete(r), Outcome::Integrity);
}
#[test]
fn test_comp_lost_commit_ack_recovers_completion() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 140);
    assert!(matches!(
        c.complete(completion(141)),
        Outcome::Committed(..)
    ));
    assert_eq!(c.complete(completion(141)), Outcome::SameCompletion(141));
}
#[test]
fn test_comp_conflicting_duplicate_completion_quarantines() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 150);
    assert!(matches!(
        c.complete(completion(151)),
        Outcome::Committed(..)
    ));
    let mut r = completion(152);
    r.output_digest = digest(b"conflict");
    assert_eq!(c.complete(r), Outcome::Quarantined(152));
}
#[test]
fn test_comp_abort_revokes_lease_and_fences() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 160);
    assert_eq!(c.abort(authority(), 161), Outcome::Aborted(161));
    assert_eq!(c.active_lease(), None);
}
#[test]
fn test_comp_completion_after_abort_rejected() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 170);
    assert!(matches!(c.abort(authority(), 171), Outcome::Aborted(_)));
    assert_eq!(
        c.complete(completion(172)),
        Outcome::Stale {
            generation: GENERATION + 1,
            lease_epoch: None
        }
    );
}
#[test]
fn test_comp_generation_fence_rejects_inflight_completion() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 180);
    c.fence(181);
    assert_eq!(
        c.complete(completion(182)),
        Outcome::Stale {
            generation: GENERATION + 1,
            lease_epoch: None
        }
    );
}
#[test]
fn test_comp_recovery_preserves_inflight_turn_unresolved() {
    let mut c = GovernedTurnHarness::new();
    start_turn(&mut c, 190);
    c.reconstruct();
    assert!(
        c.unresolved(TURN_ID),
        "recovery must not silently retry or commit an interrupted governed turn"
    );
}
