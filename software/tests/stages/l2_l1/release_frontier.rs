//! A shared-memory release joins independent execution streams.

use ktpu::{Backend, CardStack};

#[test]
fn release_waits_for_every_stream_in_its_frontier() {
    let source = include_str!("../../perf/h/gen/witness/release_frontier.ktpu");
    let stack = CardStack {
        no_passes: true,
        ..Default::default()
    };
    let card = super::common::card();
    let l2 = stack.parse_l2(source).expect("release syntax");
    let l1 = stack
        .lower(&l2, "release_frontier", &card)
        .expect("release lowering");
    let text = stack.print_l1(&l1);
    let before = text.split("ring mesh=1").next().expect("producer body");
    assert!(text.contains("ring mesh=1"), "{text}");
    for core in 0..2 {
        assert!(
            before.contains(&format!("wait vc[{core}]")),
            "release does not join vc[{core}]: {text}"
        );
    }
}
