//! An f16 A quantised in the contraction kernel (`Knobs::activation_fp16`)
//! and its quant ring (`Knobs::quant_ring`).

use super::{card, lines, text};
use ktpu_graph as g;
use ktpu_kernels::expr;
use ktpu_kernels::gen::matmul::{self as mm, Knobs};
use ktpu_kernels::gen::vector::Operand;
use ktpu_machine::PAIRING;
use serde_json::json;

fn f16(ring_depth: i64, quant_ring: i64) -> Knobs {
    Knobs {
        activation_fp16: true,
        ring_depth,
        quant_ring,
        ..mm::defaults()
    }
}

/// `bt` products of [m, k] x [256, k], a padded M tail where `m` asks.
fn product(bt: i64, m: i64, k: i64) -> mm::Mm {
    let plan = Knobs {
        pad_m: true,
        ..mm::defaults()
    };
    mm::plan(bt, m, 256, k, false, 4, PAIRING, &card(), &plan).unwrap()
}

fn port(k: &ktpu_kernels::Kernel, name: &str) -> (Vec<i64>, g::Dtype) {
    let p = k.graph.ports.iter().find(|p| p.name == name).unwrap();
    (p.spec.shape.clone(), p.spec.dtype)
}

/// A kernel takes A as f16 where its quant packs on the vector cores and
/// A's rows are whole words; the knob is otherwise off.
#[test]
fn an_f16_a_is_taken_where_the_vector_cores_pad_it() {
    let on = f16(0, 0);
    let card = card();
    assert!(mm::takes_f16(64, 2, &on, &card));
    assert!(!mm::takes_f16(72, 2, &on, &card));
    assert!(!mm::takes_f16(64, 2, &mm::defaults(), &card));
    let mover = card
        .with_fact(
            "conversion.fp16_mx7.allowed_backends",
            json!(["mover_quant", "vector_mx7"]),
        )
        .unwrap()
        .with_fact(
            "conversion.fp16_mx7.preferred_backend",
            json!("mover_quant"),
        )
        .unwrap();
    assert!(!mm::takes_f16(64, 1, &on, &mover));
    assert!(mm::takes_f16(64, 2, &on, &mover));
}

/// Port `a` is the f16 [bt * m, K]; each batch is quantised into a DRAM
/// packing first, its padding rows and K tail packed in place.
#[test]
fn an_f16_a_is_quantised_a_batch_at_a_time() {
    let p = product(2, 197, 80);
    assert!(p.mp > p.m && p.kp > p.k, "{p:?}");
    let full = Operand::full(256, 0);
    let k = mm::mm_kernel(&p, &expr::load(0), &[], &full, &f16(0, 0), &card()).unwrap();
    assert_eq!(port(&k, "a"), (vec![2 * 197, 80], g::Dtype::F16));
    let quants = lines(&k, "quant");
    assert_eq!(quants.len(), 2, "{quants:?}");
    assert!(quants
        .iter()
        .all(|q| q.contains("pad=1") && !q.contains("status")));
    let base = mm::mm_kernel(&p, &expr::load(0), &[], &full, &mm::defaults(), &card()).unwrap();
    assert_eq!(port(&base, "a").1, g::Dtype::Mx7a);
    assert!(lines(&base, "quant").is_empty());
}

/// The quant ring resolves, never refuses: none without an f16 A or a
/// product ring, at most 2, and fewer slots where the room is short.
#[test]
fn the_quant_ring_is_what_the_kernel_can_hold() {
    let p = product(1, 197, 64);
    let full = Operand::full(256, 0);
    let r = |k: &Knobs, mach: &ktpu_machine::Machine| {
        mm::ring_resolve(&p, &expr::load(0), &full, k, mach)
    };
    let card = card();
    assert_eq!(r(&f16(2, 1), &card).quant, 1);
    assert_eq!(r(&f16(2, 2), &card).quant, 2);
    assert_eq!(r(&f16(2, 7), &card).quant, 2);
    assert_eq!(r(&f16(0, 2), &card).quant, 0);
    let mx7 = Knobs {
        activation_fp16: false,
        ..f16(2, 2)
    };
    assert_eq!(r(&mx7, &card).quant, 0);
    let ring = r(&f16(2, 2), &card);
    let row_tiles = p.mp / p.tm;
    assert!(ring.qa_slots >= 1 && ring.qa_slots <= row_tiles, "{ring:?}");
    let effective = mm::effective(&p, &expr::load(0), &full, &f16(2, 7), &card);
    assert_eq!((effective.activation_fp16, effective.quant_ring), (true, 2));
}

/// Quant ring 1 quantises each row tile in the wave reaching it; 2 a wave
/// ahead, its finite status deferred into a DRAM buffer and checked once at
/// the end. Only the ring's slots are on `@stage`.
#[test]
fn the_quant_ring_quantises_row_tiles_into_stage_slots() {
    let p = product(2, 197, 80);
    let full = Operand::full(256, 0);
    let add = expr::op("add", vec![expr::load(0).into(), expr::load(1).into()]).unwrap();
    for (e, extras) in [(expr::load(0), vec![]), (add, vec![full])] {
        for q in [1, 2] {
            let k = mm::mm_kernel(&p, &e, &extras, &full, &f16(2, q), &card()).unwrap();
            let t = text(&k);
            assert_eq!(port(&k, "a").1, g::Dtype::F16);
            let quants = lines(&k, "quant");
            assert!(!quants.is_empty(), "{t}");
            assert_eq!(
                quants.iter().all(|l| l.contains("status=")),
                q == 2,
                "{quants:?}"
            );
            assert_eq!(lines(&k, "quant_check").len(), (q == 2) as usize, "{t}");
            let spaces: Vec<(g::Dtype, g::BufSpace)> = k
                .graph
                .scratch
                .iter()
                .map(|s| (s.spec.dtype, s.spec.space))
                .collect();
            assert!(
                spaces.contains(&(g::Dtype::Mx7a, g::BufSpace::Stage)),
                "{spaces:?}"
            );
            assert_eq!(
                spaces.contains(&(g::Dtype::F16, g::BufSpace::Dram)),
                q == 2,
                "{spaces:?}"
            );
        }
    }
}
