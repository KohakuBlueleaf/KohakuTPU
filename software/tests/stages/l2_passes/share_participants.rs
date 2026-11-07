//! Inferred sharing follows the instantiated physical participants.

use ktpu_l2::lower::lower;
use ktpu_l2::passes::share::share;
use ktpu_l2::target::Target;
use ktpu_l2::{Program, Search};

use super::common::{card, compare, Level, Pair};

#[test]
fn symbolic_cluster_ranges_keep_every_shared_product() {
    let source = r#"target ktpu.v9
fn participants.l2(a: mx7a[4, 64] @dram(gm=1, nk=2), b: mx7b[32, 64] @dram(gn=8, nk=2), out: f16[12, 32] @dram(tile=1x8))
    par n in 0..1 on node[n]
        par u in 0..(3+n) on mg[2-u]
            acc = zero : f32[4, 32] @acc
            av = fill a[0:4, 0:64] : mx7a[4, 64] @A
            bv = fill b[0:32, 0:64] : mx7b[32, 64] @B
            acc = gemm av, bv, acc : f32[4, 32] @acc
            drain acc -> out[u*4:+4, 0:32]
"#;
    let target = Target::of(&card());
    let program = Program::parse(source, "participants", Some("participants"))
        .expect("symbolic cluster range");
    let inferred = share(&program, &target).expect("share inference");
    let lowered = lower(&inferred, &target, &Search::default())
        .expect("physical participants admit inferred sharing");
    let pair = Pair {
        name: "participants".into(),
        before: (Level::L2, source.into()),
        after: (Level::L1, lowered),
    };
    assert_eq!(compare(&pair), Ok(None), "all three products are published");
}
