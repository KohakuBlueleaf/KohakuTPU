//! Products: API.OP.005-.007, API.FORM.006, .007, .011.

use super::{card, lines, stmts, text};
use ktpu_graph as g;
use ktpu_kernels::expr;
use ktpu_kernels::gen::matmul::{self as mm, Side};
use ktpu_kernels::gen::vector::Operand;
use ktpu_machine::{Pairing, PAIRING};

fn relu() -> expr::E {
    expr::op("max", vec![expr::load(0).into(), 0.0.into()]).unwrap()
}

#[test]
fn api_op_006_product_columns_whole_blocks() {
    let d = mm::defaults();
    let err = mm::plan(1, 64, 40, 64, true, 4, PAIRING, &card(), &d).unwrap_err();
    assert_eq!(err.clause, "API.OP.006");
    let p = mm::plan(1, 64, 96, 40, true, 4, Pairing::Vector, &card(), &d).unwrap();
    assert_eq!((p.kp, p.nk, p.tn), (64, 2, 32));
}

/// The tiling of a [m, 768] x [n, 768] product under `k`.
fn tiling(m: i64, n: i64, k: &mm::Knobs) -> mm::Mm {
    mm::plan(1, m, n, 768, true, 4, PAIRING, &card(), k).unwrap()
}

#[test]
fn pad_m_balances_the_m_tiles() {
    let d = mm::defaults();
    let base = tiling(1576, 768, &d);
    assert_eq!((base.tm, base.mp), (8, 1576), "the largest dividing tile");
    let pad = tiling(1576, 768, &mm::Knobs { pad_m: true, ..d });
    assert_eq!((pad.tm, pad.mp), (228, 1596), "seven balanced tiles");
    let even = tiling(1024, 768, &mm::Knobs { pad_m: true, ..d });
    assert_eq!(
        (even.tm, even.mp),
        (256, 1024),
        "a dividing tile prices lower"
    );
}

#[test]
fn dense_n_takes_the_widest_dividing_tile() {
    let d = mm::defaults();
    assert_eq!(tiling(64, 96, &d).tn, 32);
    assert_eq!(tiling(64, 96, &mm::Knobs { dense_n: true, ..d }).tn, 96);
}

#[test]
fn n_tiles_splits_n() {
    let d = mm::defaults();
    let one = tiling(64, 768, &d);
    let six = tiling(64, 768, &mm::Knobs { n_tiles: 6, ..d });
    assert_eq!((768 / one.tn, 768 / six.tn), (3, 6));
    let dense = mm::Knobs {
        n_tiles: 6,
        dense_n: true,
        ..d
    };
    assert_eq!(768 / tiling(64, 768, &dense).tn, 6);
}

#[test]
fn pad_k_pairs_the_k_blocks() {
    let d = mm::defaults();
    let pad = mm::Knobs { pad_k: true, ..d };
    let at = |k: i64, knobs: &mm::Knobs, m: &ktpu_machine::Machine, p: Pairing| {
        let t = mm::plan(1, 64, 128, k, true, 4, p, m, knobs).unwrap();
        (t.kp, t.nk)
    };
    let c = card();
    assert_eq!(at(96, &d, &c, Pairing::Vector), (96, 1));
    assert_eq!(at(96, &pad, &c, Pairing::Vector), (128, 2));
    assert_eq!(at(32, &pad, &c, Pairing::Vector), (32, 1), "one block");
    let mover = super::common::machine("vlt_v9t6s_1n", 1);
    assert!(!mover.pairs_blocks(Pairing::Unpaired));
    assert_eq!(at(96, &pad, &mover, Pairing::Unpaired), (96, 1), "unpaired");
}

/// The vector packing pads rows and columns in the quant (`pad=1`) and takes
/// the schedule; no scratch stands in front of it.
#[test]
fn a_vector_quant_pads_in_the_quant() {
    let src = Operand::full(96, 0);
    let wide = mm::quant_kernel(&src, 64, 96, 64, 128, Side::B, 16, 2, 1, &card()).unwrap();
    assert_eq!(
        lines(&wide, "quant"),
        vec![format!(
            "quant q[0 : +64] <- x[0 : +64, 0 : +96] {}",
            schedule()
        )]
    );
    let tall = mm::quant_kernel(&src, 40, 96, 48, 128, Side::B, 12, 2, 1, &card()).unwrap();
    assert_eq!(
        lines(&tall, "quant"),
        vec![format!(
            "quant q[0 : +48] <- x[0 : +40, 0 : +96] {}",
            schedule()
        )]
    );
    assert!(!text(&tall).contains("buffer"), "{}", text(&tall));
}

/// A mover quant packs whole views: padding goes through a scratch, and the
/// vector schedule is not written.
#[test]
fn a_mover_quant_pads_a_scratch_without_the_schedule() {
    let m = ktpu_machine::Machine::named("vlt_v9t6s_1n").unwrap();
    let src = Operand::full(96, 0);
    let padded = mm::quant_kernel(&src, 40, 96, 48, 128, Side::B, 12, 1, 1, &m).unwrap();
    assert_eq!(lines(&padded, "quant"), vec!["quant q <- h1"]);
    let whole = mm::quant_kernel(&src, 48, 96, 48, 96, Side::B, 12, 1, 1, &m).unwrap();
    assert_eq!(
        lines(&whole, "quant"),
        vec!["quant q[0 : +48] <- x[0 : +48, 0 : +96]"]
    );
}

/// Two batches of one product tile each: a batch at a time one cluster
/// works, with batch_tiles both clusters work at once; a single batch's
/// kernel is the same either way.
#[test]
fn batch_tiles_shares_out_every_batchs_tiles() {
    let d = mm::defaults();
    let on = mm::Knobs {
        batch_tiles: true,
        ..d
    };
    let p = mm::plan(2, 8, 32, 64, false, 4, PAIRING, &card(), &d).unwrap();
    assert_eq!((p.mp / p.tm) * (p.n / p.tn), 1, "one tile a batch");
    let kernel = |p: &mm::Mm, k: &mm::Knobs| {
        let out = Operand::full(p.n, p.bt * p.mp);
        mm::mm_kernel(p, &relu(), &[], &out, k, &card()).unwrap()
    };
    let mg = |k: &mm::Knobs| -> Vec<String> {
        lines(&kernel(&p, k), "par")
            .into_iter()
            .filter(|l| l.contains(" on mg"))
            .collect()
    };
    assert_eq!(mg(&d), vec!["par u1 in 0..1 on mg[u1]"]);
    assert_eq!(mg(&on), vec!["par u1 in 0..2 on mg[u1]"]);
    let one = mm::plan(1, 8, 32, 64, false, 4, PAIRING, &card(), &d).unwrap();
    assert_eq!(kernel(&one, &d).graph, kernel(&one, &on).graph);
    // A recorded call replays the knob it names, and none where it names none.
    let mut call = serde_json::json!({
        "mm": {"made_by": {"gen": "matmul.plan",
            "args": {"bt": 2, "m": 8, "n": 32, "k": 64, "shared": false}}},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 2 * p.mp},
    });
    let load = |k: &mm::Knobs| {
        let out = Operand::full(p.n, p.bt * p.mp);
        mm::mm_kernel(&p, &expr::load(0), &[], &out, k, &card())
            .unwrap()
            .graph
    };
    let replay = |c: &serde_json::Value| {
        ktpu_kernels::call::call("matmul.mm_kernel", c, &card())
            .unwrap()
            .graph
    };
    assert_eq!(replay(&call), load(&d));
    call["batch_tiles"] = true.into();
    assert_eq!(replay(&call), load(&on));
    assert_ne!(load(&on), load(&d));
}

/// The default vector quant's keywords as the L2 text writes them.
fn schedule() -> &'static str {
    "finite_batch=4 pipeline=1 zero_classifier=1 stream_axes=1 fill_overlap=1 zero_reuse=1 pad=1 overlap=1"
}

/// A call records its group's knobs by name, and a recorded call plans
/// under the knobs it names, the contraction before the knobs where it
/// names none.
#[test]
fn a_call_records_and_replays_its_knobs() {
    let k = mm::Knobs {
        pad_m: true,
        n_tiles: 2,
        dense_n: true,
        pad_k: true,
        batch_tiles: true,
        ring_depth: 2,
        split_vc: true,
        tail_bands: true,
        activation_fp16: true,
        quant_ring: 2,
        expr_cse: true,
    };
    let args: serde_json::Map<String, serde_json::Value> = k
        .args(mm::Group::Plan)
        .into_iter()
        .map(|(n, v)| (n.to_string(), v))
        .collect();
    assert_eq!(
        serde_json::Value::Object(args.clone()),
        serde_json::json!({"pad_m": true, "n_tiles": 2, "dense_n": true, "pad_k": true})
    );
    assert_eq!(
        k.args(mm::Group::Kernel),
        vec![
            ("batch_tiles", serde_json::json!(true)),
            ("ring_depth", serde_json::json!(2)),
            ("split_vc", serde_json::json!(true)),
            ("tail_bands", serde_json::json!(true)),
            ("activation_fp16", serde_json::json!(true)),
            ("quant_ring", serde_json::json!(2)),
            ("expr_cse", serde_json::json!(true)),
        ]
    );
    let mut call = serde_json::json!({"bt": 1, "m": 1576, "n": 768, "k": 768});
    let bare = ktpu_kernels::call::mm_plan(&call, &card()).unwrap();
    assert_eq!(bare, tiling(1576, 768, &mm::unrecorded()));
    call.as_object_mut().unwrap().extend(args);
    let recorded = ktpu_kernels::call::mm_plan(&call, &card()).unwrap();
    assert_eq!(recorded, tiling(1576, 768, &k));
    assert_ne!(recorded, bare);
}

#[test]
fn api_op_007_product_and_epilogue_one_kernel() {
    let p = mm::plan(1, 64, 128, 64, true, 4, PAIRING, &card(), &mm::defaults()).unwrap();
    let out = Operand::full(128, 64);
    let k = mm::mm_kernel(&p, &relu(), &[], &out, &mm::defaults(), &card()).unwrap();
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

/// Under expr_cse an epilogue's structurally equal subexpressions are one
/// value: (x + 1)(x + 1) built twice computes like (x + 1) shared, and an
/// epilogue with nothing to merge takes the knob as off.
#[test]
fn expr_cse_computes_equal_subexpressions_once() {
    let d = mm::defaults();
    let cse = mm::Knobs {
        expr_cse: true,
        ..d
    };
    let p = mm::plan(1, 64, 128, 64, true, 4, PAIRING, &card(), &d).unwrap();
    let out = Operand::full(128, 64);
    let inc = || expr::op("add", vec![expr::load(0).into(), 1.0.into()]).unwrap();
    let twice = expr::op("mul", vec![inc().into(), inc().into()]).unwrap();
    let a = inc();
    let shared = expr::op("mul", vec![a.clone().into(), a.into()]).unwrap();
    let kernel = |e: &expr::E, k: &mm::Knobs| mm::mm_kernel(&p, e, &[], &out, k, &card()).unwrap();
    let adds = |k: &ktpu_kernels::kernel::Kernel| text(k).matches("= add v").count();
    assert_eq!(adds(&kernel(&twice, &d)), 2);
    assert_eq!(adds(&kernel(&twice, &cse)), 1);
    assert_eq!(
        kernel(&twice, &cse).graph.body,
        kernel(&shared, &d).graph.body
    );
    assert!(mm::effective(&p, &twice, &out, &cse, &card()).expr_cse);
    assert!(!mm::effective(&p, &shared, &out, &cse, &card()).expr_cse);
    assert_eq!(kernel(&shared, &cse).graph, kernel(&shared, &d).graph);
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
    assert!(
        t.contains("quant q[48 : +48] <- x[40 : +40, 0 : +64]"),
        "{t}"
    );
}

#[test]
fn api_form_007_rows_not_whole_words_refused() {
    let src = Operand::full(10, 0);
    let err = mm::quant_kernel(&src, 8, 10, 8, 32, Side::B, 8, 1, 1, &card()).unwrap_err();
    assert_eq!(err.clause, "API.FORM.007");
}

#[test]
fn api_form_011_tile_scratch_read_gt4() {
    let p = mm::plan(1, 64, 128, 64, true, 4, PAIRING, &card(), &mm::defaults()).unwrap();
    let k = mm::mm_kernel(
        &p,
        &relu(),
        &[],
        &Operand::full(128, 64),
        &mm::defaults(),
        &card(),
    )
    .unwrap();
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
