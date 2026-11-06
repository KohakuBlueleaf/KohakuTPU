//! Recycling of a two-slot stream whose first receive has no consumers.
use ktpu::{Backend, CardStack};

#[test]
fn unused_receives_release_their_stream_slot() {
    for (side, dtype) in [("A", "mx7a"), ("B", "mx7b")] {
        let source = format!(
            r#"target ktpu.v9
fn recycle.l2(x: f16[4, 32] @dram)
    par c in 0..1 on vc[c]
        for t in 0..4
            h = load x : f16[4, 32] @l1(gt4)
            v = unpack h : f32[4, 32] @reg(gt4)
            q = pack v : {dtype}[4, 32] @l1
            send q -> mg[0] side={side}
    par u in 0..1 on mg[u]
        for t in 0..4
            a = recv vc[0] : {dtype}[4, 32] @{side}
"#
        );
        let stack = CardStack {
            no_passes: true,
            ..Default::default()
        };
        let card = super::common::card();
        let l2 = stack.parse_l2(&source).expect("stream syntax");
        let l1 = stack.lower(&l2, "recycle", &card).expect("stream lowering");
        let text = stack.print_l1(&l1);
        let lines: Vec<_> = text.lines().map(str::trim).collect();
        let first = lines
            .iter()
            .position(|line| line.starts_with(&format!("fill {side}")) && line.contains(" recv "))
            .expect("first receive");
        assert_eq!(lines[first + 1], "post n=4 to=vc[0]", "{text}");
        // The sender can deliver a tile only with four credits. The receiver
        // consumes one delivered tile at each FILL and returns only its POSTs.
        let (mut credit, mut sent, mut waiting, mut received) = (0, 0, 0, 0);
        for line in lines {
            if line.starts_with("post n=") && line.ends_with("to=vc[0]") {
                let amount = line
                    .split_whitespace()
                    .nth(1)
                    .expect("amount")
                    .trim_start_matches("n=")
                    .parse::<usize>()
                    .expect("integer");
                credit += amount;
            }
            while credit >= 4 && sent < 4 {
                credit -= 4;
                sent += 1;
                waiting += 1;
            }
            if line.starts_with(&format!("fill {side}")) && line.contains(" recv ") {
                assert!(
                    waiting > 0,
                    "receiver starved after {received} tiles: {text}"
                );
                waiting -= 1;
                received += 1;
            }
        }
        assert_eq!((sent, received, waiting, credit), (4, 4, 0, 0));
    }
}
