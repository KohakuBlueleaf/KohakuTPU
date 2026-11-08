//! Independently packed B chunks remain live across complete ordered row products.
use super::common::{judge, Fixture};

#[test]
fn bank_slots_keep_three_weight_chunks_through_two_complete_products() {
    let fixture = Fixture {
        label: "resident-b-bank-slots".into(),
        name: "resident_b".into(),
        source: r#"target ktpu.v9
fn resident_b.l2(a: mx7a[8,96] @dram(gm=2,nk=1), b: mx7b[8,96] @dram(gn=1,nk=1), out: f16[8,8] @dram(tile=1x2))
    par u in 0..1 on mg[u]
        b0 = fill b[0:8,0:32] : mx7b[8,32] @B
        b1 = fill b[0:8,32:64] : mx7b[8,32] @B
        b2 = fill b[0:8,64:96] : mx7b[8,32] @B
        c0 = zero : f32[4,8] @acc
        a0 = fill a[0:4,0:32] : mx7a[4,32] @A
        c0 = gemm a0,b0,c0 : f32[4,8] @acc
        a1 = fill a[0:4,32:64] : mx7a[4,32] @A
        c0 = gemm a1,b1,c0 : f32[4,8] @acc
        a2 = fill a[0:4,64:96] : mx7a[4,32] @A
        c0 = gemm a2,b2,c0 : f32[4,8] @acc
        drain c0 -> out[0:4,0:8]
        c1 = zero : f32[4,8] @acc
        a3 = fill a[4:8,0:32] : mx7a[4,32] @A
        c1 = gemm a3,b0,c1 : f32[4,8] @acc
        a4 = fill a[4:8,32:64] : mx7a[4,32] @A
        c1 = gemm a4,b1,c1 : f32[4,8] @acc
        a5 = fill a[4:8,64:96] : mx7a[4,32] @A
        c1 = gemm a5,b2,c1 : f32[4,8] @acc
        drain c1 -> out[4:8,0:8]
"#
        .into(),
    };
    let pair = super::lower(&fixture, &super::common::card()).expect("retained B bank slots");
    judge(&[(fixture.label, Ok(pair))]);
}
