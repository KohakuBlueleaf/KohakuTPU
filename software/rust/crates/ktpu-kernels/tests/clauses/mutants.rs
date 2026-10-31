//! Each spec vector that calls a generator fails under the mutant it
//! names (`software/tests/spec/vectors/api`): a vector its mutant cannot
//! break would be a finding. A refusal under the mutant breaks it too.

use super::rows;
use ktpu_kernels::mutants::with;
use std::panic::{catch_unwind, AssertUnwindSafe};

fn breaks<T: PartialEq + std::fmt::Debug>(name: &'static str, facts: fn() -> T) {
    let want = facts();
    let got = catch_unwind(AssertUnwindSafe(|| with(name, facts)));
    if let Ok(got) = got {
        assert_ne!(got, want, "mutant {name} does not break the facts");
    }
}

#[test]
fn api_row_001_breaks_under_port_pad() {
    breaks("api.vector/port_pad", rows::row_001_facts);
}

#[test]
fn api_row_002_breaks_under_bands_one() {
    breaks("api.vector/bands_one", rows::row_002_facts);
}

#[test]
fn api_row_004_breaks_under_stages_whole() {
    breaks("api.vector/stages_whole", rows::row_004_facts);
}

#[test]
fn api_row_006_breaks_under_l1_small() {
    breaks("api.vector/l1_small", rows::row_006_facts);
}

#[test]
fn dsl_kern_016_breaks_under_name() {
    breaks("dsl.kernel/name", super::moves::kern_016_facts);
}

#[test]
fn api_row_004_past_r_breaks_under_stages_whole() {
    breaks("api.vector/stages_whole", rows::row_004_past_r_facts);
}

/// A full output packs the statistic directly instead of materialising rows.
#[test]
fn rul_030_breaks_under_stat_rows_form() {
    breaks(
        "kernels.vector/stat_rows_form",
        super::rulings::rul_030_facts,
    );
}

/// A reduction read twice taken twice.
#[test]
fn reduces_breaks_under_reduces_dup() {
    breaks("kernels.expr/reduces_dup", super::expr::reduces_facts);
}

#[test]
fn api_row_007_breaks_under_split_one() {
    breaks("api.vector/split_one", rows::row_007_facts);
}

#[test]
fn api_form_010_breaks_under_stat_rows() {
    breaks("api.vector/stat_rows", rows::form_010_facts);
}

/// Two members of one fused rows stage: under the mutant the second writes
/// nothing.
fn fused_facts() -> ktpu_kernels::kernel::Kernel {
    use ktpu_graph::Dtype;
    use ktpu_kernels::expr;
    use ktpu_kernels::gen::fused::{kernel, Named, Package, Step};
    use ktpu_kernels::gen::vector::Operand;
    let m = super::card();
    let full = Operand::full(128, 8);
    let port = |name: &str, written| {
        let shape = vec![8, 128];
        let name = name.to_string();
        (
            Named {
                name,
                shape,
                dtype: Dtype::F16,
            },
            written,
        )
    };
    let neg = expr::op("neg", vec![expr::load(0).into()]).unwrap();
    let names = |n: &[&str]| n.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    let package = Package {
        ports: vec![port("p0", false), port("p1", true), port("p2", true)],
        scratch: vec![],
        staged: vec![],
        steps: vec![Step::Rows {
            r: 8,
            w: 128,
            exprs: vec![neg, expr::load(0)],
            operands: vec![full],
            ins: names(&["p0"]),
            out_operands: vec![full, full],
            outs: names(&["p1", "p2"]),
        }],
    };
    kernel(&package, "two-members", &m).unwrap()
}

#[test]
fn fused_members_break_under_lost_member() {
    use ktpu_kernels::gen::fused::LOST_MEMBER_MUTANT;
    breaks(LOST_MEMBER_MUTANT, fused_facts);
}

/// A class rows kernel carries its band schedule.
fn class_rows_facts() -> ktpu_kernels::kernel::Kernel {
    use ktpu_kernels::expr;
    use ktpu_kernels::gen::classes::{rows, Cores, Policy, Rows, ShapeClass, SCHEDULE};
    use ktpu_kernels::gen::vector::Operand;
    let class = ShapeClass {
        kblock: 32,
        max_block: 256,
        policy: Policy::Pow2,
        cores: Cores::All,
    };
    let (full, e) = (Operand::full(128, 32), expr::load(0));
    let a = Rows {
        e: &e,
        operands: &[full],
        r: 32,
        w: 128,
        out: &full,
        bands: None,
        class: &class,
        schedule: &SCHEDULE,
    };
    rows::kernel(&a, false, &super::card()).unwrap().kernel
}

#[test]
fn class_rows_break_under_bandless() {
    use ktpu_kernels::trace::control::BANDLESS_MUTANT;
    breaks(BANDLESS_MUTANT, class_rows_facts);
}

/// A walked packing on the vector cores carries the quant schedule.
fn quant_walk_facts() -> ktpu_kernels::kernel::Kernel {
    use ktpu_kernels::gen::matmul::{quant_walk_kernel, Side, QUANT_SCHEDULE};
    let m = super::card();
    quant_walk_kernel(
        480,
        &[(15, 32)],
        0,
        5,
        48,
        8,
        64,
        Side::A,
        2,
        2,
        1,
        &m,
        false,
        QUANT_SCHEDULE,
    )
    .unwrap()
}

#[test]
fn quant_walk_breaks_under_no_schedule() {
    breaks("kernels.quant/no-schedule", quant_walk_facts);
}

/// A rows stage of trips the card's two vector cores do not divide evenly
/// deals them differently under the other split; one of even trips alike.
#[test]
fn odd_trips_break_under_other_split() {
    use ktpu_kernels::expr;
    use ktpu_kernels::gen::vector::{plan, rows_kernel, Kind, Operand, OTHER_SPLIT_MUTANT};
    use ktpu_kernels::gen::Hw;
    let m = super::card();
    let hw = Hw::of(&m);
    let full = |r| Operand::full(1024, r);
    let trips = |r: i64| {
        plan(&[full(r)], Kind::Full, r, 1024, None, None, hw.band, &hw)
            .unwrap()
            .nb
    };
    let kernel = |r: i64| {
        let m = m.clone();
        move || {
            let e = expr::op("neg", vec![expr::load(0).into()]).unwrap();
            rows_kernel(&e, &[full(r)], r, 1024, &full(r), None, &m).unwrap()
        }
    };
    let rows: Vec<i64> = (1..64).map(|k| k * hw.band).collect();
    let odd = *rows
        .iter()
        .find(|r| trips(**r) > 2 && trips(**r) % 2 == 1)
        .unwrap();
    let even = *rows
        .iter()
        .find(|r| trips(**r) > 2 && trips(**r) % 2 == 0)
        .unwrap();
    assert_ne!(with(OTHER_SPLIT_MUTANT, kernel(odd)), kernel(odd)());
    assert_eq!(with(OTHER_SPLIT_MUTANT, kernel(even)), kernel(even)());
}

/// A made operand is written where it is read; copied, it holds one buffer
/// more, of the port's size.
#[test]
fn a_made_operand_copied_holds_a_second_buffer() {
    use ktpu_kernels::gen::prepared::COPIED_MUTANT;
    let full = serde_json::json!({
        "kind": "full", "dtype": "f16", "pitch": 64, "offset": 0, "rows": 0,
    });
    let args = serde_json::json!({
        "e": {"nodes": [{"load": 0}, {"const": 2.0}, {"op": "mul", "args": [0, 1]}], "root": 2},
        "operands": [full], "R": 88, "W": 64, "out": full, "alias": {},
        "made": {"0": [{"made_by": {"gen": "relayout.plan", "args": {
            "src_n": 3520, "walk": [[88, 40], [40, 1]], "at": 0,
            "dwalk": [[88, 64], [40, 1]], "dat": 0, "m": 5632, "keep": false,
        }}}]},
    });
    let m = super::card();
    let scratch = || {
        let k = ktpu_kernels::call::call("prepared.rows_kernel", &args, &m).unwrap();
        serde_json::to_value(&k.graph).unwrap()["scratch"]
            .as_array()
            .unwrap()
            .len()
    };
    assert_eq!(with(COPIED_MUTANT, scratch), scratch() + 1);
}

/// API.ROW.003's vector runs the planner (`tg.run`); at the generator its
/// mutant lets a row of 288 columns (no block of 64..256 divides it) be
/// one block, not nine of 32.
#[test]
fn api_row_003_breaks_under_max_block() {
    use ktpu_kernels::expr;
    use ktpu_kernels::gen::vector::{rows_kernel, Operand};
    let blocks = || {
        let e = expr::op("neg", vec![expr::load(0).into()]).unwrap();
        let full = Operand::full(288, 8);
        rows_kernel(&e, &[full], 8, 288, &full, None, &super::card()).map(|(_, n)| n)
    };
    assert_eq!(blocks(), Ok(9));
    assert_eq!(with("api.rows/max_block", blocks), Ok(1));
}
