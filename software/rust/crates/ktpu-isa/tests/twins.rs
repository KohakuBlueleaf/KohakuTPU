//! The `encode.vector` mutants the tier-1 isa vectors name
//! (`runners/isa.py`): each changes the value Python's patch changes, by
//! the same amount, and nothing without it.
#![cfg(feature = "mutants")]

use ktpu_isa::mutant::with;
use ktpu_isa::unit::vector::core::{e8m15, Src, VWord};
use ktpu_isa::unit::vector::{dim, VecIsa};
use ktpu_isa::Machine;

#[test]
fn e8m15_f16_flips_mantissa_bit_7() {
    // 1.0 is FP32 0x3F800000; E8M15 keeps its top 24 bits.
    assert_eq!(e8m15(1.0), 0x3F_8000);
    assert_eq!(with("isa.vector/e8m15_f16", || e8m15(1.0)), 0x3F_8080);
    assert_eq!(
        with("isa.vector/e8m15_f16", || e8m15(-2.5)),
        e8m15(-2.5) ^ 1 << 7
    );
}

#[test]
fn dim_word_flips_stride_bit_5() {
    assert_eq!(dim(1, 5).unwrap(), 1 << 16 | 5);
    assert_eq!(
        with("isa.vector/dim_word", || dim(1, 5)).unwrap(),
        33 << 16 | 5
    );
    assert_eq!(
        with("isa.vector/dim_word", || dim(-1, 0)).unwrap(),
        (0x3FFFF ^ 32) << 16
    );
}

#[test]
fn lower_adds_16_to_every_stride() {
    assert_eq!(
        with("isa.vector/lower", || dim(1, 5)).unwrap(),
        17 << 16 | 5
    );
    assert_eq!(with("isa.vector/lower", || dim(-16, 7)).unwrap(), 7);
}

#[test]
fn src_sel_codes_s_and_k_as_2() {
    assert_eq!([Src::V.code(), Src::S.code(), Src::K.code()], [0, 1, 3]);
    let got = with("isa.vector/src_sel", || {
        [Src::V.code(), Src::S.code(), Src::K.code()]
    });
    assert_eq!(got, [0, 2, 2]);
}

#[test]
fn xbar_c_drops_the_c_crossbar_bit() {
    let isa = VecIsa::new(&Machine::named("multimesh_v9t6").unwrap()).unwrap();
    let xb = |xc| VWord::Xb {
        xm: 2,
        xk: 3,
        sh: 0,
        xc,
        pm: 1,
        pr: 2,
    };
    let (with_c, without_c) = (
        xb(true).encode(&isa).unwrap(),
        xb(false).encode(&isa).unwrap(),
    );
    assert_ne!(with_c, without_c);
    assert_eq!(
        with("isa.vector/xbar_c", || xb(true).encode(&isa)).unwrap(),
        without_c
    );
    assert_eq!(
        with("isa.vector/xbar_c", || xb(false).encode(&isa)).unwrap(),
        without_c
    );
}

#[test]
fn every_twin_is_carried() {
    for n in ["src_sel", "e8m15_f16", "dim_word", "xbar_c", "lower"] {
        let n = format!("isa.vector/{n}");
        assert!(ktpu_isa::mutant::NAMES.contains(&n.as_str()), "{n}");
    }
}
