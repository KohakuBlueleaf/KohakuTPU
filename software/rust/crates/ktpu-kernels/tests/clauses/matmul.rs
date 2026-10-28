//! Products: API.OP.005-.007, API.FORM.006, .007, .011.

use super::{card, stmts, text};
use ktpu_graph as g;
use ktpu_kernels::expr;
use ktpu_kernels::gen::matmul::{self as mm, Side};
use ktpu_kernels::gen::vector::Operand;

fn relu() -> expr::E {
    expr::op("max", vec![expr::load(0).into(), 0.0.into()]).unwrap()
}

#[test]
fn api_op_006_product_columns_whole_blocks() {
    let err = mm::plan(1, 64, 40, 64, true, 4, &card()).unwrap_err();
    assert_eq!(err.clause, "API.OP.006");
    let p = mm::plan(1, 64, 96, 40, true, 4, &card()).unwrap();
    assert_eq!((p.kp, p.nk, p.tn), (64, 2, 32));
}

#[test]
fn api_op_007_product_and_epilogue_one_kernel() {
    let p = mm::plan(1, 64, 128, 64, true, 4, &card()).unwrap();
    let out = Operand::full(128, 64);
    let k = mm::mm_kernel(&p, &relu(), &[], &out, &card()).unwrap();
    let units: Vec<g::UnitKind> = stmts(&k.graph.body)
        .into_iter()
        .filter_map(|s| match &s.node {
            g::Node::Par { unit, .. } => Some(unit.kind),
            _ => None,
        })
        .collect();
    assert!(units.contains(&g::UnitKind::Mg) && units.contains(&g::UnitKind::Vc));
    let t = text(&k);
    for op in ["= fill a", "= gemm a", "drain acc", "= max v"] {
        assert!(t.contains(op), "no {op} in\n{t}");
    }
}

#[test]
fn api_form_006_mxfp7_packing() {
    let src = Operand::full(64, 0);
    let k = mm::quant_kernel(&src, 40, 64, 48, 64, Side::A, 12, 2, 2, &card()).unwrap();
    let q = k.graph.ports.iter().find(|p| p.name == "q").unwrap();
    assert_eq!(
        ktpu_graph::text::spec(&q.spec),
        "mx7a[96, 64] @dram(gm=12, nk=2)"
    );
    let t = text(&k);
    assert!(t.contains("fill h1 value=0"), "{t}");
    assert!(
        t.contains("copy h1[48 : +40, 0 : +64] <- x[40 : +40, 0 : +64]"),
        "{t}"
    );
    assert!(t.contains("quant q <- h1"), "{t}");
}

#[test]
fn api_form_007_rows_not_whole_words_refused() {
    let src = Operand::full(10, 0);
    let err = mm::quant_kernel(&src, 8, 10, 8, 32, Side::B, 8, 1, 1, &card()).unwrap_err();
    assert_eq!(err.clause, "API.FORM.007");
}

#[test]
fn api_form_011_tile_scratch_read_gt4() {
    let p = mm::plan(1, 64, 128, 64, true, 4, &card()).unwrap();
    let k = mm::mm_kernel(&p, &relu(), &[], &Operand::full(128, 64), &card()).unwrap();
    let h = &k.graph.scratch[0];
    assert_eq!(
        h.spec.layout,
        vec![(
            "tile".to_string(),
            g::LayoutValue::Ints(vec![p.tm / 4, p.tn / 4])
        )]
    );
    let loads_h: Vec<_> = stmts(&k.graph.body)
        .into_iter()
        .filter_map(|s| match &s.node {
            g::Node::Value {
                op: g::ValueOp::Load,
                ty,
                args,
                ..
            } if matches!(&args[0], g::Operand::View(v) if v.buffer == h.name) => Some(ty.mode),
            _ => None,
        })
        .collect();
    assert!(!loads_h.is_empty() && loads_h.iter().all(|m| *m == Some(g::Mode::Gt4)));
}
