//! `ktpu::mutant`: a build without the `mutants` feature carries none and
//! refuses every name (`_rs.compile(mutant=)` raises, never runs unmutated);
//! with it, every stage crate's names, and a name none carries refused.

#![cfg(feature = "stack")]

use ktpu::mutant;

#[cfg(not(feature = "mutants"))]
#[test]
fn a_build_without_mutants_carries_none_and_refuses_every_name() {
    assert!(mutant::names().is_empty());
    for name in ["isa.vector/dim", "isa.cluster/fields", "l0.lower/no_rel"] {
        let got = mutant::with(name, || ());
        assert!(got.is_err(), "{name}: {got:?}");
        assert!(got.unwrap_err().contains("no `mutants` feature"));
    }
}

#[cfg(feature = "mutants")]
#[test]
fn a_mutant_build_switches_the_named_mutant_in_its_crate() {
    let names = mutant::names();
    for n in ["isa.vector/dim", "isa.cluster/fields", "l0.lower/no_rel"] {
        assert!(names.contains(&n), "{n} not carried");
    }
    let seen = mutant::with("isa.vector/dim", || ktpu_isa::mutant::on("isa.vector/dim"));
    assert_eq!(seen, Ok(true));
    assert!(
        !ktpu_isa::mutant::on("isa.vector/dim"),
        "switched off after"
    );
    assert!(mutant::with("no.such/mutant", || ()).is_err());
}
