//! T-320-D1 contract: every new Action has one immutable payload and actuator binding.

use castor_kernel::c01_storage::{
    ActionBinding, AppendConditionalOutcome, AppendConditionalRequest, CoreEntry, D1DurableStorage,
    DurabilityProfile, DurableStorage, EnsureRegionOutcome,
};
use castor_kernel::c06_composition::{
    ActionRegistrationRequest, AdmitTurnRequest, CapabilityGrant, CapabilityRight,
    CommitTurnRequest, ConsumeInteractionRequest, D1GovernedTurnAuthority, GovernedTurnOutcome,
    GrantCapabilityRequest, InteractionOutcomeReport, PresentAdmissionCertificateRequest,
    RequestInteractionRequest,
};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use tempfile::TempDir;

const AGENT: &str = "agent-binding-test";
const ACTUATOR: &str = "repo-workspace-actuator";

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn binding(action_id: &str, region_ref: &str, bytes: &[u8]) -> ActionBinding {
    ActionBinding {
        action_id: action_id.into(),
        payload_region_ref: region_ref.into(),
        payload_digest: digest(bytes),
        actuator_id: ACTUATOR.into(),
    }
}

struct Fixture {
    root: TempDir,
    authority: D1GovernedTurnAuthority,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("temporary D1 root");
        let mut storage = D1DurableStorage::open(root.path()).expect("open D1 storage");
        for (region, bytes) in [
            ("region://successor-1", b"successor-one".as_slice()),
            ("region://successor-2", b"successor-two".as_slice()),
            (
                "region://manifest-1",
                b"write-client\nwrite-tests".as_slice(),
            ),
            ("region://manifest-2", b"write-client".as_slice()),
            ("region://payload-client-v1", b"client-v1".as_slice()),
            ("region://payload-client-v2", b"client-v2".as_slice()),
            ("region://payload-tests", b"tests-v1".as_slice()),
            ("region://observation-1", b"observation-one".as_slice()),
            ("region://observation-2", b"observation-two".as_slice()),
        ] {
            assert!(matches!(
                storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
                EnsureRegionOutcome::Success(_)
            ));
        }
        Self {
            root,
            authority: D1GovernedTurnAuthority::for_test(storage),
        }
    }

    fn ready_turn(&mut self, turn_id: u64, base: &str, lease_epoch: u64) {
        assert!(matches!(
            self.authority.admit_turn(AdmitTurnRequest {
                agent_id: AGENT.into(),
                turn_id,
                lease_epoch: 0,
                base_projection_digest: base.into(),
                cap_id: None,
            }),
            GovernedTurnOutcome::Admitted { .. }
        ));
        let interaction_id = format!("interaction-{turn_id}");
        assert_eq!(
            self.authority
                .request_interaction(RequestInteractionRequest {
                    query_operation: None,
                    interaction_id: interaction_id.clone(),
                    lease_epoch: 0,
                    request_digest: digest(b"request"),
                }),
            GovernedTurnOutcome::InteractionRequested
        );
        let (observation_region_id, observation_bytes) = if turn_id == 1 {
            ("region://observation-1", b"observation-one".as_slice())
        } else {
            ("region://observation-2", b"observation-two".as_slice())
        };
        assert_eq!(
            self.authority.report_outcome(InteractionOutcomeReport {
                interaction_id: interaction_id.clone(),
                observation_region_id: observation_region_id.into(),
                observation_digest: digest(observation_bytes),
            }),
            GovernedTurnOutcome::InteractionBound
        );
        assert!(matches!(
            self.authority
                .consume_interaction(ConsumeInteractionRequest {
                    interaction_id,
                    lease_epoch,
                }),
            GovernedTurnOutcome::InteractionConsumed(_)
        ));
    }

    fn first_commit(&mut self, bindings: Vec<ActionBinding>) -> GovernedTurnOutcome {
        self.authority.commit_turn(CommitTurnRequest {
            lease_epoch: 1,
            base_projection_digest: digest(b"base-zero"),
            successor_region_id: "region://successor-1".into(),
            successor_digest: digest(b"successor-one"),
            action_manifest_region_id: "region://manifest-1".into(),
            action_manifest_digest: digest(b"write-client\nwrite-tests"),
            action_manifest: vec!["write-client".into(), "write-tests".into()],
            action_bindings: bindings,
            cap_id: None,
        })
    }
}

fn registration(action_id: &str, actuator_id: &str) -> ActionRegistrationRequest {
    ActionRegistrationRequest {
        stable_operation_id: Some(format!("operation-{action_id}")),
        action_id: action_id.into(),
        agent_id: AGENT.into(),
        action_family: actuator_id.into(),
        cap_id: String::new(),
        target_scope: format!("repo:castor:file/{action_id}"),
        numeric_parameters: BTreeMap::new(),
        exact_parameters: BTreeMap::new(),
    }
}

fn admission(action_id: &str) -> PresentAdmissionCertificateRequest {
    PresentAdmissionCertificateRequest {
        action_id: action_id.into(),
        target_scope: format!("repo:castor:file/{action_id}"),
        capability_id: String::new(),
        generation: 1,
    }
}

#[test]
fn committed_binding_survives_replay_and_arms_exact_payload() {
    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    assert_eq!(
        fixture.first_commit(vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::TurnCommitted
    );
    let root = fixture.root.path().to_path_buf();
    drop(fixture.authority);
    let mut authority = D1GovernedTurnAuthority::open(root).expect("replay bound actions");
    assert!(matches!(
        authority.admit_turn(AdmitTurnRequest {
            agent_id: AGENT.into(),
            turn_id: 2,
            lease_epoch: 0,
            base_projection_digest: digest(b"successor-one"),
            cap_id: None,
        }),
        GovernedTurnOutcome::Admitted { .. }
    ));
    assert_eq!(
        authority.register_action(registration("write-client", ACTUATOR)),
        GovernedTurnOutcome::ActionRegistered
    );
    assert_eq!(
        authority.present_admission_certificate(admission("write-client")),
        GovernedTurnOutcome::AttemptArmed { attempt_id: 1 }
    );
    assert!(authority.inspect_journal().iter().any(|entry| matches!(
        entry,
        CoreEntry::AttemptArmed {
            action_id,
            action_region_ref,
            action_digest,
            actuator_id: Some(actuator_id),
            ..
        } if action_id == "write-client"
            && action_region_ref == "region://payload-client-v1"
            && action_digest == &digest(b"client-v1")
            && actuator_id == ACTUATOR
    )));
}

#[test]
fn manifest_and_binding_ids_must_be_identical_and_unique() {
    let cases = [
        vec![binding(
            "write-client",
            "region://payload-client-v1",
            b"client-v1",
        )],
        vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("extra", "region://payload-tests", b"tests-v1"),
        ],
        vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("write-client", "region://payload-client-v1", b"client-v1"),
        ],
    ];
    for bindings in cases {
        let mut fixture = Fixture::new();
        fixture.ready_turn(1, &digest(b"base-zero"), 1);
        assert_eq!(
            fixture.first_commit(bindings),
            GovernedTurnOutcome::RejectedPrecondition
        );
    }
}

#[test]
fn binding_requires_persisted_matching_payload_and_nonempty_actuator() {
    let cases = [
        vec![
            binding("write-client", "region://missing", b"missing"),
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ],
        vec![
            ActionBinding {
                payload_digest: digest(b"wrong-client"),
                ..binding("write-client", "region://payload-client-v1", b"client-v1")
            },
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ],
    ];
    for bindings in cases {
        let mut fixture = Fixture::new();
        fixture.ready_turn(1, &digest(b"base-zero"), 1);
        assert_eq!(
            fixture.first_commit(bindings),
            GovernedTurnOutcome::IntegrityOrProtocolFault
        );
    }

    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    let mut empty_actuator = binding("write-client", "region://payload-client-v1", b"client-v1");
    empty_actuator.actuator_id.clear();
    assert_eq!(
        fixture.first_commit(vec![
            empty_actuator,
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn registration_must_match_committed_actuator_and_action_cannot_rebind() {
    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    assert_eq!(
        fixture.first_commit(vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::TurnCommitted
    );
    assert_eq!(
        fixture
            .authority
            .register_action(registration("write-client", "different-actuator")),
        GovernedTurnOutcome::RejectedPrecondition
    );
    fixture.ready_turn(2, &digest(b"successor-one"), 2);
    let before_rebind = fixture.authority.inspect_journal().len();
    assert_eq!(
        fixture.authority.commit_turn(CommitTurnRequest {
            lease_epoch: 2,
            base_projection_digest: digest(b"successor-one"),
            successor_region_id: "region://successor-2".into(),
            successor_digest: digest(b"successor-two"),
            action_manifest_region_id: "region://manifest-2".into(),
            action_manifest_digest: digest(b"write-client"),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![binding(
                "write-client",
                "region://payload-client-v2",
                b"client-v2",
            )],
            cap_id: None,
        }),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(fixture.authority.inspect_journal().len(), before_rebind);
}

#[test]
fn committed_action_accepts_identical_republication_but_rejects_actuator_only_rebind() {
    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    assert_eq!(
        fixture.first_commit(vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::TurnCommitted
    );

    fixture.ready_turn(2, &digest(b"successor-one"), 2);
    let same_binding = binding("write-client", "region://payload-client-v1", b"client-v1");
    assert_eq!(
        fixture.authority.commit_turn(CommitTurnRequest {
            lease_epoch: 2,
            base_projection_digest: digest(b"successor-one"),
            successor_region_id: "region://successor-2".into(),
            successor_digest: digest(b"successor-two"),
            action_manifest_region_id: "region://manifest-2".into(),
            action_manifest_digest: digest(b"write-client"),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![same_binding],
            cap_id: None,
        }),
        GovernedTurnOutcome::TurnCommitted
    );

    fixture.ready_turn(3, &digest(b"successor-two"), 3);
    let before_rebind = fixture.authority.inspect_journal().len();
    let mut actuator_rebind = binding("write-client", "region://payload-client-v1", b"client-v1");
    actuator_rebind.actuator_id = "different-actuator".into();
    assert_eq!(
        fixture.authority.commit_turn(CommitTurnRequest {
            lease_epoch: 3,
            base_projection_digest: digest(b"successor-two"),
            successor_region_id: "region://successor-2".into(),
            successor_digest: digest(b"successor-two"),
            action_manifest_region_id: "region://manifest-2".into(),
            action_manifest_digest: digest(b"write-client"),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![actuator_rebind],
            cap_id: None,
        }),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(fixture.authority.inspect_journal().len(), before_rebind);
}

#[test]
fn registration_capability_object_must_equal_committed_actuator() {
    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    assert_eq!(
        fixture.first_commit(vec![
            binding("write-client", "region://payload-client-v1", b"client-v1"),
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::TurnCommitted
    );

    for (cap_id, object_ref) in [
        ("cap-wrong-actuator", "different-actuator"),
        ("cap-bound-actuator", ACTUATOR),
    ] {
        assert_eq!(
            fixture.authority.grant_capability(GrantCapabilityRequest {
                grant: CapabilityGrant {
                    cap_id: cap_id.into(),
                    subject: AGENT.into(),
                    object_ref: object_ref.into(),
                    rights: vec![CapabilityRight::RegisterAction],
                    constraints: vec![],
                    parent_cap_id: None,
                    revocation_domain: None,
                    delegation_allowed: false,
                    max_turns: None,
                },
            }),
            GovernedTurnOutcome::CapabilityGranted
        );
    }

    let mut wrong = registration("write-client", ACTUATOR);
    wrong.cap_id = "cap-wrong-actuator".into();
    let before_rejection = fixture.authority.inspect_journal().len();
    assert_eq!(
        fixture.authority.register_action(wrong),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(fixture.authority.inspect_journal().len(), before_rejection);

    let mut matching = registration("write-client", ACTUATOR);
    matching.cap_id = "cap-bound-actuator".into();
    assert_eq!(
        fixture.authority.register_action(matching),
        GovernedTurnOutcome::ActionRegistered
    );
}

#[test]
fn legacy_unbound_history_replays_but_cannot_authorize_attempt() {
    let missing_bindings_json = serde_json::json!({
        "TurnCommitted": {
            "turn_id": 1,
            "successor_projection_digest": digest(b"successor-one"),
            "action_manifest_digest": digest(b"write-client"),
            "action_manifest": ["write-client"],
            "cap_id": null
        }
    });
    let decoded: CoreEntry = serde_json::from_value(missing_bindings_json)
        .expect("historical TurnCommitted decodes without action_bindings");
    assert!(matches!(
        decoded,
        CoreEntry::TurnCommitted {
            action_bindings,
            ..
        } if action_bindings.is_empty()
    ));

    let root = tempfile::tempdir().expect("legacy journal root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open legacy storage");
    for (region, bytes) in [
        ("region://successor-1", b"successor-one".as_slice()),
        ("region://manifest-2", b"write-client".as_slice()),
    ] {
        assert!(matches!(
            storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
            EnsureRegionOutcome::Success(_)
        ));
    }
    let first = AppendConditionalRequest {
        agent_id: AGENT.into(),
        entry_id: 1,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: Some(1),
        expected_lease_epoch: Some(1),
        expected_base_projection_digest: Some(digest(b"base-zero")),
        entry: CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: Some(digest(b"successor-one")),
            action_manifest_digest: Some(digest(b"write-client")),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![],
            cap_id: None,
        },
        region_refs: vec!["region://successor-1".into(), "region://manifest-2".into()],
    };
    let proof = match storage.append_conditional(first) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof,
        other => panic!("legacy TurnCommitted must persist: {other:?}"),
    };
    let registration_entry = AppendConditionalRequest {
        agent_id: AGENT.into(),
        entry_id: 2,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: None,
        expected_lease_epoch: None,
        expected_base_projection_digest: Some(digest(b"successor-one")),
        entry: CoreEntry::ActionRegistered {
            stable_operation_id: None,
            action_id: "write-client".into(),
            cap_id: String::new(),
            target_scope: Some("repo:castor:file/write-client".into()),
        },
        region_refs: vec![],
    };
    assert_eq!(proof.expected_projection_digest, digest(b"base-zero"));
    assert!(matches!(
        storage.append_conditional(registration_entry),
        AppendConditionalOutcome::EntryPersisted(_)
    ));
    drop(storage);

    let mut authority = D1GovernedTurnAuthority::open(root.path()).expect("replay legacy history");
    assert_eq!(
        authority.present_admission_certificate(admission("write-client")),
        GovernedTurnOutcome::RejectedPrecondition
    );
}

#[test]
fn durable_store_requires_every_binding_region_and_digest_in_the_commit_proof() {
    for (include_payload_ref, payload_digest) in [
        (false, digest(b"client-v1")),
        (true, digest(b"different-client")),
    ] {
        let root = tempfile::tempdir().expect("binding proof root");
        let mut storage = D1DurableStorage::open(root.path()).expect("open binding proof store");
        for (region, bytes) in [
            ("region://successor-1", b"successor-one".as_slice()),
            ("region://manifest-2", b"write-client".as_slice()),
            ("region://payload-client-v1", b"client-v1".as_slice()),
        ] {
            assert!(matches!(
                storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
                EnsureRegionOutcome::Success(_)
            ));
        }
        let mut region_refs = vec!["region://successor-1".into(), "region://manifest-2".into()];
        if include_payload_ref {
            region_refs.push("region://payload-client-v1".into());
        }
        let request = AppendConditionalRequest {
            agent_id: AGENT.into(),
            entry_id: 1,
            expected_core_epoch: 1,
            expected_agent_generation: Some(1),
            expected_turn_id: Some(1),
            expected_lease_epoch: Some(1),
            expected_base_projection_digest: Some(digest(b"base-zero")),
            entry: CoreEntry::TurnCommitted {
                turn_id: 1,
                successor_projection_digest: Some(digest(b"successor-one")),
                action_manifest_digest: Some(digest(b"write-client")),
                action_manifest: vec!["write-client".into()],
                action_bindings: vec![ActionBinding {
                    action_id: "write-client".into(),
                    payload_region_ref: "region://payload-client-v1".into(),
                    payload_digest,
                    actuator_id: ACTUATOR.into(),
                }],
                cap_id: None,
            },
            region_refs,
        };
        assert_eq!(
            storage.append_conditional(request),
            AppendConditionalOutcome::IntegrityFault
        );
    }
}

fn append_direct(
    storage: &mut D1DurableStorage,
    entry_id: u64,
    base: &str,
    entry: CoreEntry,
    region_refs: Vec<String>,
) -> String {
    match storage.append_conditional(AppendConditionalRequest {
        agent_id: AGENT.into(),
        entry_id,
        expected_core_epoch: 1,
        expected_agent_generation: Some(1),
        expected_turn_id: None,
        expected_lease_epoch: None,
        expected_base_projection_digest: Some(base.into()),
        entry,
        region_refs,
    }) {
        AppendConditionalOutcome::EntryPersisted(proof) => proof.entry_digest,
        other => panic!("direct history entry must persist: {other:?}"),
    }
}

fn persist_replay_regions(storage: &mut D1DurableStorage) {
    for (region, bytes) in [
        ("region://successor-1", b"successor-one".as_slice()),
        ("region://successor-2", b"successor-two".as_slice()),
        ("region://manifest-2", b"write-client".as_slice()),
        ("region://payload-client-v1", b"client-v1".as_slice()),
        ("region://payload-client-v2", b"client-v2".as_slice()),
    ] {
        assert!(matches!(
            storage.ensure_region(region, &digest(bytes), bytes, DurabilityProfile::D1),
            EnsureRegionOutcome::Success(_)
        ));
    }
}

fn append_replayed_registration(
    storage: &mut D1DurableStorage,
    entry_id: u64,
    base: &str,
) -> String {
    append_direct(
        storage,
        entry_id,
        base,
        CoreEntry::ActionRegistered {
            stable_operation_id: None,
            action_id: "write-client".into(),
            cap_id: String::new(),
            target_scope: Some("repo:castor:file/write-client".into()),
        },
        vec![],
    )
}

fn assert_replayed_history_cannot_arm(root: &TempDir, base: String, turn_id: u64) {
    let mut authority = D1GovernedTurnAuthority::open(root.path()).expect("replay direct history");
    assert!(matches!(
        authority.admit_turn(AdmitTurnRequest {
            agent_id: AGENT.into(),
            turn_id,
            lease_epoch: 0,
            base_projection_digest: base,
            cap_id: None,
        }),
        GovernedTurnOutcome::Admitted { .. }
    ));
    let before = authority.inspect_journal().len();
    assert_eq!(
        authority.present_admission_certificate(admission("write-client")),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(authority.inspect_journal().len(), before);
}

#[test]
fn replayed_duplicate_binding_ids_are_unusable_instead_of_last_wins() {
    let root = tempfile::tempdir().expect("duplicate binding history root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open direct history");
    persist_replay_regions(&mut storage);
    append_direct(
        &mut storage,
        1,
        &digest(b"base-zero"),
        CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: Some(digest(b"successor-one")),
            action_manifest_digest: Some(digest(b"write-client")),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![
                binding("write-client", "region://payload-client-v1", b"client-v1"),
                binding("write-client", "region://payload-client-v2", b"client-v2"),
            ],
            cap_id: None,
        },
        vec![
            "region://successor-1".into(),
            "region://manifest-2".into(),
            "region://payload-client-v1".into(),
            "region://payload-client-v2".into(),
        ],
    );
    let recovery_base = append_replayed_registration(&mut storage, 2, &digest(b"successor-one"));
    drop(storage);
    assert_replayed_history_cannot_arm(&root, recovery_base, 2);
}

#[test]
fn replayed_cross_turn_binding_conflict_is_unusable_instead_of_overwritten() {
    let root = tempfile::tempdir().expect("conflicting binding history root");
    let mut storage = D1DurableStorage::open(root.path()).expect("open direct history");
    persist_replay_regions(&mut storage);
    append_direct(
        &mut storage,
        1,
        &digest(b"base-zero"),
        CoreEntry::TurnCommitted {
            turn_id: 1,
            successor_projection_digest: Some(digest(b"successor-one")),
            action_manifest_digest: Some(digest(b"write-client")),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![binding(
                "write-client",
                "region://payload-client-v1",
                b"client-v1",
            )],
            cap_id: None,
        },
        vec![
            "region://successor-1".into(),
            "region://manifest-2".into(),
            "region://payload-client-v1".into(),
        ],
    );
    append_direct(
        &mut storage,
        2,
        &digest(b"successor-one"),
        CoreEntry::TurnCommitted {
            turn_id: 2,
            successor_projection_digest: Some(digest(b"successor-two")),
            action_manifest_digest: Some(digest(b"write-client")),
            action_manifest: vec!["write-client".into()],
            action_bindings: vec![binding(
                "write-client",
                "region://payload-client-v2",
                b"client-v2",
            )],
            cap_id: None,
        },
        vec![
            "region://successor-2".into(),
            "region://manifest-2".into(),
            "region://payload-client-v2".into(),
        ],
    );
    let recovery_base = append_replayed_registration(&mut storage, 3, &digest(b"successor-two"));
    drop(storage);
    assert_replayed_history_cannot_arm(&root, recovery_base, 3);
}

#[test]
fn whitespace_only_actuator_identity_is_rejected_without_journal_mutation() {
    let mut fixture = Fixture::new();
    fixture.ready_turn(1, &digest(b"base-zero"), 1);
    let before = fixture.authority.inspect_journal().len();
    let mut whitespace = binding("write-client", "region://payload-client-v1", b"client-v1");
    whitespace.actuator_id = " \t".into();
    assert_eq!(
        fixture.first_commit(vec![
            whitespace,
            binding("write-tests", "region://payload-tests", b"tests-v1"),
        ]),
        GovernedTurnOutcome::RejectedPrecondition
    );
    assert_eq!(fixture.authority.inspect_journal().len(), before);
}
