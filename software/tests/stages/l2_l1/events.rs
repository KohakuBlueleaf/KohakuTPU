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

#[test]
fn native_tree_consumes_each_subtotal_before_forwarding_or_reopening() {
    let fixture = super::common::Fixture {
        label: "native-tree".into(),
        name: "native_tree".into(),
        source: r#"target ktpu.v9
fn native_tree.l2(x: mx7a[4, 128] @dram(gm=1, nk=1), w: mx7b[32, 128] @dram(gn=8, nk=1), y: f16[4, 32] @dram(tile=1x8))
    for tile in 0..1
        par n in 0..4 on node[n]
            par u in 0..1 on mg[u]
                acc = zero : f32[4, 32] @acc
                a = fill x[:, n*32 : +32] : mx7a[4, 32] @A
                b = fill w[:, n*32 : +32] : mx7b[32, 32] @B
                acc = gemm a, b, acc : f32[4, 32] @acc
                when n == 0
                    drain acc -> mg[0] add node=1
                when n == 3
                    drain acc -> mg[0] add node=2
                when n == 1
                    drain acc -> mg[0] add node=2
                when n == 2
                    drain acc -> y
"#
        .into(),
    };
    let pair = super::lower(&fixture, &super::common::card()).expect("native tree lowering");
    let text = &pair.after.1;
    let node = |id| {
        let marker = format!("    node {id}\n");
        let start = text.find(&marker).expect("node scope") + marker.len();
        let body = &text[start..];
        &body[..body.find("\n    node ").unwrap_or(body.len())]
    };
    let intermediate = node(1);
    let consume = intermediate
        .find("fill A0 recv n=1")
        .expect("incoming marker");
    let forward = intermediate
        .find("to=mesh2.mg[0] buf=2")
        .expect("native forwarding");
    assert!(consume < forward, "{text}");
    let root = node(2);
    let first = root.find("ring mesh=3").expect("leaf ready");
    let consume = root.find("fill A0 recv n=1").expect("leaf completion");
    let second = root.find("ring mesh=1").expect("subtotal ready");
    assert!(
        first < consume && consume < second,
        "one receive context: {text}"
    );
    assert!(
        !root.contains(" fused") && !root.contains(" emit="),
        "plain root drain: {text}"
    );
    for line in text
        .lines()
        .filter(|line| line.trim_start().starts_with("ring "))
    {
        let slot = line
            .split_whitespace()
            .find_map(|s| s.strip_prefix("slot="))
            .expect("lease")
            .parse::<i64>()
            .expect("numeric lease");
        assert!((9..16).contains(&slot), "{text}");
    }
    assert!(
        super::common::compare(&pair)
            .expect("both simulations terminate")
            .is_none(),
        "native FP22 tree agrees with L2: {text}"
    );
}
