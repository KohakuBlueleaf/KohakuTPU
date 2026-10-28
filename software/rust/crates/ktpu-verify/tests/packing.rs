//! Neither MOVER nor lowered ENGINE bypasses board conversion admission.
use ktpu_isa::{Move, MoveMode, MoverIsa, Walk};
use ktpu_l0::{Facts, PackageBuilder};
use ktpu_machine::Machine;
use ktpu_verify::{lines, Target};
use std::collections::BTreeMap;

#[test]
fn forbidden_quant_go_and_lowered_engine_are_refused() {
    let m = Machine::named("vlt_v9t6s_1n").unwrap();
    let f = Facts::of(&m);
    let mut b = PackageBuilder::new(&f, BTreeMap::new(), 32);
    let mv = Move {
        mode: MoveMode::Xform,
        src: Some(Walk::new(0x10000, &[(8, 32)])),
        dst: Some(Walk::new(0x20000, &[(1, 128)])),
        ewidth: 2,
        flags: 8,
        xform: (1, 0),
        imm: None,
        seed: None,
        index: None,
        row: None,
        fp: false,
    };
    let writes: Vec<_> = MoverIsa::of(&m)
        .move_writes(&mv)
        .unwrap()
        .into_iter()
        .map(|(r, v)| (u64::from(r), v))
        .collect();
    b.mover(&writes, None, false);
    let p = b.build(false, false);
    let mut restricted = Target::from_machine(&m, None).unwrap();
    restricted.facts.mover_quant_allowed = false;
    for pkg in [p.clone(), ktpu_l0::engine::lower(&p, &f).unwrap()] {
        assert!(lines::admit(&pkg, 0, &Target::from_machine(&m, None).unwrap()).is_ok());
        assert!(lines::admit(&pkg, 0, &restricted)
            .unwrap_err()
            .contains("forbids mover_quant GO"));
        #[cfg(feature = "mutants")]
        ktpu_verify::mutant::with("verify.conversion/ignore-policy", || {
            assert!(lines::admit(&pkg, 0, &restricted).is_ok());
        });
    }
}
