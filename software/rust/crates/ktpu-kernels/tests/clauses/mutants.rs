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
