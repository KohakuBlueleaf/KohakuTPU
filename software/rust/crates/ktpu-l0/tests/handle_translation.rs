use ktpu_l0::format::bind::translate;
use ktpu_l0::{Buffer, Kind};

fn buffer() -> Buffer {
    Buffer {
        name: "x".into(),
        size: 32,
        default: 99,
        kind: Kind::Temp,
        mesh: 0,
    }
}

#[test]
fn handles_translate_views_and_address_zero() {
    assert_eq!(
        translate(&[buffer()], &[0x1020], &[(0x1000, 0x400000, 128)]).unwrap(),
        vec![0x400020]
    );
    assert_eq!(
        translate(&[buffer()], &[0x1000], &[(0x1000, 0, 128)]).unwrap(),
        vec![0]
    );
}

#[test]
fn handles_refuse_missing_stale_overflow_and_extent() {
    for (handles, table) in [
        (vec![], vec![(0x1000, 0, 128)]),
        (vec![0x1000], vec![]),
        (vec![0x1070], vec![(0x1000, 0, 128)]),
        (vec![0x1000], vec![(0x1000, u64::MAX - 31, 128)]),
        (vec![0x1000], vec![(0x1000, 0, 128), (0x1040, 4096, 32)]),
    ] {
        assert_eq!(
            translate(&[buffer()], &handles, &table).unwrap_err().clause,
            "RUL.023"
        );
    }
}

#[cfg(feature = "mutants")]
#[test]
fn identity_mutant_changes_physical_binding() {
    let got = ktpu_l0::mutant::with("l0.mmu/identity", || {
        translate(&[buffer()], &[0x1000], &[(0x1000, 0, 128)]).unwrap()
    });
    assert_ne!(got, vec![0]);
}
