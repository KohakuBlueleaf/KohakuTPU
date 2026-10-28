//! Rulings the generators follow: RUL.016, .017, .018, .030, .060.

use super::{card, floats, stmts, text};
use ktpu_graph as g;
use ktpu_kernels::expr::{self, RedOp};
use ktpu_kernels::gen::lanes;
use ktpu_kernels::gen::vector::{self as v, Operand};
use ktpu_kernels::trace::Idx;
use ktpu_kernels::Kind;
use ktpu_machine::Machine;
use serde_json::json;

fn with(m: &Machine, fact: &str, value: serde_json::Value) -> Machine {
    m.with_fact(fact, value).unwrap()
}

#[test]
fn rul_016_split_covers_every_trip_once() {
    for cores in 1..=4 {
        for n in 1..=40 {
            let sp = v::split(n, cores);
            assert!(sp.cores <= cores);
            let mut seen = vec![0; n as usize];
            for c in 0..sp.cores {
                let c = Idx::Int(c);
                let (s, t) = (sp.start(&c).int().unwrap(), sp.trips(&c).int().unwrap());
                assert!(t >= 1, "n {n} cores {cores}: core {c:?} runs {t}");
                for i in s..s + t {
                    seen[i as usize] += 1;
                }
            }
            assert!(
                seen.iter().all(|x| *x == 1),
                "n {n} cores {cores}: {seen:?}"
            );
        }
    }
}

#[test]
fn rul_016_cores_from_the_machine() {
    let four = with(&card(), "vc.count", json!(4));
    let full = Operand::full(64, 64);
    let e = expr::op("neg", vec![expr::load(0).into()]).unwrap();
    let (k, _) = v::rows_kernel(&e, &[full], 64, 64, &full, None, &four).unwrap();
    let his: Vec<g::Index> = stmts(&k.graph.body)
        .into_iter()
        .filter_map(|s| match &s.node {
            g::Node::Par { hi, unit, .. } if unit.kind == g::UnitKind::Vc => Some(hi.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(his, vec![g::Index::Int(4)]);
    assert_eq!(k.graph.machine.vc, 4);
}

#[test]
fn rul_017_relayout_pass_from_descriptors() {
    let pl = lanes::plan(448, &[(64, 7)], 0, &[(64, 1)], 0, 64, false, &card()).unwrap();
    let passes = |m: &Machine| {
        let k = lanes::relayout_kernel(&pl, m).unwrap();
        stmts(&k.graph.body)
            .into_iter()
            .filter(|s| matches!(&s.node, g::Node::Par { .. }))
            .count()
    };
    let six = with(&card(), "vc.descriptors", json!(6));
    assert!(
        passes(&six) > passes(&card()),
        "{} vs {}",
        passes(&six),
        passes(&card())
    );
}

#[test]
fn rul_018_lanes_from_the_machine() {
    let eight = with(&card(), "vc.lanes", json!(8));
    let e = expr::op("mul", vec![expr::load(0).into(), expr::load(1).into()]).unwrap();
    let k = v::flat_kernel(
        &e,
        &[g::Dtype::F16; 2],
        1024,
        g::Dtype::F16,
        &[(1, 5)],
        &eight,
    )
    .unwrap();
    assert_eq!(k.graph.ports[1].spec.shape, vec![8]);
    let err = v::flat_kernel(
        &e,
        &[g::Dtype::F16; 2],
        1024,
        g::Dtype::F16,
        &[(1, 9)],
        &eight,
    )
    .unwrap_err();
    assert_eq!(err.clause, "DSL.VEC.003");
}

#[test]
fn rul_010_target_from_the_machine() {
    let k = ktpu_kernels::copy_kernel(64, &card()).unwrap();
    assert_eq!(k.graph.target, "ktpu.v9");
    let other = with(&card(), "die.target", json!("ktpu.v10"));
    let k = ktpu_kernels::copy_kernel(64, &other).unwrap();
    assert_eq!(k.graph.target, "ktpu.v10");
}

/// A full output materialises compact statistics as ordinary register rows.
pub fn rul_030_facts() -> Option<String> {
    let e = expr::reduce(RedOp::Sum, expr::load(0));
    let full = Operand::full(64, 16);
    v::rows_kernel(&e, &[full], 16, 64, &full, None, &card())
        .ok()
        .map(|(k, _)| text(&k))
}

#[test]
fn rul_030_no_rows_form_of_a_statistic() {
    let full = rul_030_facts().expect("a full output materialises register rows");
    assert!(full.contains("mul s1[:, *], 1.0"), "{full}");
    assert!(!full.contains("pack s1"), "{full}");
    let e = expr::reduce(RedOp::Sum, expr::load(0));
    let full = Operand::full(64, 16);
    let (k, _) =
        v::rows_kernel(&e, &[full], 16, 64, &Operand::new(Kind::Row), None, &card()).unwrap();
    assert!(text(&k).contains(": f16[16] @l1"), "{}", text(&k));
}

#[test]
fn rul_060_trunc_whole_from_sig_bits() {
    let e = expr::op("trunc", vec![expr::load(0).into()]).unwrap();
    let k = v::flat_kernel(&e, &[g::Dtype::F16], 128, g::Dtype::F16, &[], &card()).unwrap();
    let f = floats(&k);
    assert!(f.contains(&32768.0) && f.contains(&-32768.0), "{f:?}");
    assert!(!f.contains(&16384.0) && !f.contains(&49152.0), "{f:?}");
    let wide = with(&card(), "vc.sig_bits", json!(18));
    let k = v::flat_kernel(&e, &[g::Dtype::F16], 128, g::Dtype::F16, &[], &wide).unwrap();
    assert!(floats(&k).contains(&131072.0));
}

#[test]
fn rul_060_fractional_range_in_lane_arithmetic() {
    use ktpu_kernels::expr::emit::{trunc, Lanes};
    for bits in [12, 16, 18] {
        let value = |x: f64| {
            if x == 0.0 {
                return x;
            }
            let e = ((x.abs().to_bits() >> 52) & 0x7FF) as i32 - 1023;
            let scale = 2f64.powi(bits - 1 - e);
            (x * scale).round_ties_even() / scale
        };
        let mut lanes = Lanes {
            value,
            sig_bits: bits as i64,
        };
        let limit = 1i32 << (bits - 1);
        for i in -2 * limit..2 * limit {
            let x = i as f64 * 0.5;
            assert_eq!(
                trunc(&mut lanes, x).unwrap(),
                x.trunc(),
                "bits={bits} x={x}"
            );
        }
        #[cfg(feature = "mutants")]
        {
            let x = -2.5;
            let bad = ktpu_kernels::mutants::with("num.expr/round-constant-2-15", || {
                trunc(&mut lanes, x).unwrap()
            });
            assert_ne!(bad, x.trunc());
        }
    }
}
