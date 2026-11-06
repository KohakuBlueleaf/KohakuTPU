//! Receiver-scoped leases preserve independent overlapping event streams.

use ktpu_l2::lower::events::{allocate_slots, BackendSyncFacts, EventId, EventKind, EventSpec};

#[test]
fn overlapping_edges_do_not_share_or_borrow_slots() {
    let event = |id: &str, lifetime| EventSpec {
        id: EventId(id.into()),
        producers: vec![0],
        consumers: vec![2],
        released_regions: vec![],
        acquire_regions: vec![],
        kind: EventKind::MemoryRelease,
        count: 1,
        lifetime,
    };
    let facts = BackendSyncFacts::common(16);
    let events = vec![event("a", 0..3), event("b", 1..4), event("c", 4..5)];
    let leases = allocate_slots(&events, &facts).expect("leases");
    assert_ne!(leases[0].slot, leases[1].slot);
    assert_eq!(
        leases[0].slot, leases[2].slot,
        "quiescent epoch permits reuse"
    );
    assert!(leases
        .iter()
        .all(|l| !facts.reserved_slots.contains(&l.slot)));
    let crowded: Vec<_> = (0..=facts.available_slots.len())
        .map(|i| event(&format!("e{i}"), 0..1))
        .collect();
    assert!(
        allocate_slots(&crowded, &facts).is_err(),
        "no reserved spill"
    );
}

#[test]
fn native_add_example_uses_only_common_backend_leases() {
    // Above the fixture corpus's size bound, so read from its example file.
    let path = super::common::software()
        .join("kohakutpu/language/kohakutpu/language/examples/l2/multinode/xsplitk.ktpu");
    let fixture = super::common::Fixture {
        label: "multinode/xsplitk".into(),
        source: std::fs::read_to_string(path).expect("the example"),
        name: "xsplitk".into(),
    };
    let text = super::lower(&fixture, &super::common::card())
        .expect("it lowers")
        .after
        .1;
    let mut rings = Vec::new();
    let mut waits = Vec::new();
    for line in text.lines() {
        let words: Vec<_> = line.split_whitespace().collect();
        if words.first() == Some(&"ring") {
            let slot = words
                .iter()
                .find_map(|s| s.strip_prefix("slot="))
                .expect("ring slot")
                .parse::<i64>()
                .expect("numeric lease");
            rings.push(slot);
        } else if words.starts_with(&["wait", "slot"]) {
            waits.push(words[2].parse::<i64>().expect("numeric lease"));
        }
    }
    assert!(
        !rings.is_empty() && !waits.is_empty(),
        "native add handshake: {text}"
    );
    assert!(
        rings.iter().chain(&waits).all(|s| (9..16).contains(s)),
        "{text}"
    );
    assert!(
        rings.iter().all(|s| waits.contains(s)),
        "each ready event is consumed"
    );
}
