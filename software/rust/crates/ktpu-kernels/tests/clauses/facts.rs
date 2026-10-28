//! The unit facts the generators size by come from the machine
//! (`mg.kblock`, `mg.lanes`, `mg.kstep`): a variant machine changes the
//! kernel or refuses it; under `kernels.hw/unit_constants` (the facts read
//! as constants) it does not.

use super::{card, text};
use ktpu_kernels::expr;
use ktpu_kernels::gen::{matmul, transpose, vector as v, Hw};
use ktpu_machine::Machine;
use serde_json::json;

fn with(fact: &str, value: i64) -> Machine {
    card().with_fact(fact, json!(value)).unwrap()
}

/// K chunks (`nk`) of a K = 128 product.
fn nk(m: &Machine) -> i64 {
    matmul::plan(1, 64, 64, 128, true, 4, m).unwrap().nk
}

/// Rows of a [64, 32] rows trip, or the refusal's clause.
fn trip_rows(m: &Machine) -> Result<i64, String> {
    let full = v::Operand::full(32, 64);
    let e = expr::op("neg", vec![expr::load(0).into()]).unwrap();
    v::plan(&[full], v::Kind::Full, 64, 32, None, None, 4, &Hw::of(m))
        .map(|p| p.rows)
        .map_err(|r| r.clause)
        .and_then(|rows| {
            v::rows_kernel(&e, &[full], 64, 32, &full, None, m)
                .map(|_| rows)
                .map_err(|r| r.clause)
        })
}

#[test]
fn kstep_from_the_machine() {
    assert_eq!(nk(&card()), 2);
    assert_eq!(nk(&with("mg.kstep", 1)), 1);
    let k = matmul::plan(1, 64, 64, 128, true, 4, &with("mg.kstep", 4)).unwrap();
    assert_eq!(k.nk, 4);
}

#[test]
fn kblock_from_the_machine() {
    assert_eq!(trip_rows(&card()), Ok(16));
    assert_eq!(trip_rows(&with("mg.kblock", 64)), Err("API.ROW.008".into()));
    let err = matmul::plan(1, 64, 96, 64, true, 4, &with("mg.kblock", 64)).unwrap_err();
    assert_eq!(err.clause, "API.OP.006");
}

#[test]
fn band_from_the_machine() {
    let eight = with("mg.lanes", 8);
    assert_eq!(trip_rows(&eight), Ok(32));
    assert_eq!(Hw::of(&eight).max_block(), 128);
    // A word of 16 lanes is not 8 x 8 sub-tiles.
    let err = transpose::transpose_kernel(1, 32, 32, &eight).unwrap_err();
    assert_eq!(err.clause, "TG.RUN.006");
    let k = transpose::transpose_kernel(1, 32, 32, &card()).unwrap();
    assert!(text(&k).contains("xlane"));
}

#[cfg(feature = "mutants")]
#[test]
fn unit_constants_breaks_the_variants() {
    use ktpu_kernels::mutants::with as mutant;
    let name = "kernels.hw/unit_constants";
    assert_eq!(mutant(name, || nk(&with("mg.kstep", 1))), 2);
    assert_eq!(mutant(name, || trip_rows(&with("mg.kblock", 64))), Ok(16));
    assert_eq!(mutant(name, || trip_rows(&with("mg.lanes", 8))), Ok(16));
}
