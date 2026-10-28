//! docs/spec/stack/rulings.md: the rulings L0 implements, each witnessed on
//! the API (a refusal names the ruling).

use super::*;
use ktpu_l0::format::{signature, Op, UNBOUND, UNBOUNDED};
use ktpu_l0::units::machine_units;
use ktpu_l0::{engine, replay, Buffer, Kind, Program, Reloc, Step};

fn bound_package(addend: i128, width: u64) -> Package {
    let mut p = pkg(
        vec![with_port(mg(1, 1))],
        vec![Step::new(Op::Dispatch, 0, 1, 0, 0)],
        vec![0],
    );
    p.buffers.push(Buffer {
        name: "a".into(),
        size: 64,
        default: 0x1000,
        kind: Kind::Temp,
        mesh: 0,
    });
    p.relocs.push(Reloc {
        payload: 0,
        bit: 0,
        width,
        buffer: 0,
        addend,
        shift: 0,
    });
    p
}

#[test]
fn rul_004_spec_requires_explicit_credit() {
    let input = serde_json::json!({"builder": {}, "calls": []});
    let run = || ktpu_l0::spec::run("l0.pkg.build", &input, None, None).unwrap();
    let refused = serde_json::json!({"refused": "TypeError"});
    assert_eq!(run(), refused);
    #[cfg(feature = "mutants")]
    {
        let broken = ktpu_l0::mutant::with("l0.build/default_credit", run);
        assert!(broken.get("package").is_some());
        assert_ne!(broken, refused);
        assert_eq!(run(), refused);
    }
}

#[test]
fn rul_004_fetch_room_is_inst_depth() {
    let mut f = f();
    f.inst_depth = 300;
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![Step::new(Op::Dispatch, 0, 400, 0, 0)],
        vec![0; 400],
    );
    let low = engine::lower(&p, &f).unwrap();
    let waits: Vec<u64> = low
        .steps
        .iter()
        .flat_map(|s| engine::entries(&low, s))
        .filter(|e| e.0 == engine::WAIT)
        .map(|e| e.1)
        .collect();
    assert_eq!(waits, [255 + 145 - 300, 400]);
}

#[test]
fn rul_005_units_past_the_smallest_bound_refused() {
    let f = f();
    assert_eq!(f.unit_bound(), 16);
    let p = pkg((0..17).map(|x| mg(x, 0)).collect(), vec![], vec![]);
    assert_eq!(p.to_bytes(&f).unwrap_err().clause, "RUL.005");
    let p = pkg((0..16).map(|x| mg(x, 0)).collect(), vec![], vec![]);
    assert!(p.to_bytes(&f).is_ok());
}

#[test]
fn rul_039_addend_outside_buffer_refused() {
    let err = bound_package(100, 40).bind(&[0x8000]).unwrap_err();
    assert_eq!(err.clause, "RUL.039");
    assert!(bound_package(63, 40).bind(&[0x8000]).is_ok());
}

#[test]
fn rul_039_address_wider_than_field_refused() {
    let err = bound_package(0, 8).bind(&[0x8000]).unwrap_err();
    assert_eq!(err.clause, "RUL.039");
    assert!(bound_package(0, 16).bind(&[0x8000]).is_ok());
}

#[test]
fn rul_039_address_arithmetic_cannot_overflow() {
    for width in [40, 64] {
        assert_eq!(
            bound_package(8, width)
                .bind(&[u64::MAX - 4])
                .unwrap_err()
                .clause,
            "RUL.039"
        );
    }
    assert!(bound_package(8, 64).bind(&[u64::MAX - 8]).is_ok());
    assert!(bound_package(-1, 64).bind(&[0]).is_err());
}

#[cfg(feature = "mutants")]
#[test]
fn ruling_mutants_break_the_checks() {
    use ktpu_l0::mutant::with;
    for (name, check) in [
        (
            "l0.lower/fetch_depth",
            rul_004_fetch_room_is_inst_depth as fn(),
        ),
        (
            "l0.pkg/unlimited_units",
            rul_005_units_past_the_smallest_bound_refused,
        ),
        ("l0.bind/unchecked", rul_039_addend_outside_buffer_refused),
        (
            "l0.bind/unchecked",
            rul_039_address_wider_than_field_refused,
        ),
        (
            "l0.bind/unchecked",
            rul_039_address_arithmetic_cannot_overflow,
        ),
        (
            "l0.bind/zero_unbound",
            rul_040_address_zero_binds_unbound_takes_default,
        ),
        (
            "l0.program/no_signature",
            rul_041_program_writes_machine_signature,
        ),
        (
            "l0.lower/rings_in_engine",
            rul_046_rings_past_the_queue_run_on_the_firmware_path,
        ),
    ] {
        check();
        assert!(
            std::panic::catch_unwind(|| with(name, check)).is_err(),
            "{name} survived"
        );
    }
}

#[test]
fn rul_040_address_zero_binds_unbound_takes_default() {
    let p = bound_package(8, 40);
    assert_eq!(p.bind(&[0]).unwrap().payloads[0], Word::from_u64(8));
    assert_eq!(
        p.bind(&[UNBOUND]).unwrap().payloads[0],
        Word::from_u64(0x1008)
    );
}

#[test]
fn rul_041_program_writes_machine_signature() {
    let f = f();
    let mut p = Program::new();
    p.send((1, 1), vec![Word::from_u64(1)], vec![]);
    let b = p.build(&f, Default::default()).unwrap();
    let sig = b.build(true, false).signature;
    assert_eq!(sig, signature(&machine_units(&f).unwrap()));
    assert_eq!(sig, 0xc7f3_fefb_01ee_41cd);
}

#[test]
fn rul_042_one_unbounded_value() {
    let mut u = mg(1, 1);
    u.credit = 0;
    let p = pkg(
        vec![u],
        vec![Step::new(Op::Dispatch, 0, 3, 0, 0)],
        vec![1, 2, 3],
    );
    let low = engine::lower(&p, &f()).unwrap();
    let credit_waits = low
        .steps
        .iter()
        .flat_map(|s| engine::entries(&low, s))
        .filter(|e| e.0 == engine::WAIT && e.1 != 3)
        .count();
    assert_eq!(credit_waits, 0);
    assert_eq!(UNBOUNDED, 1 << 32);
}

/// `n` back-to-back rings, lowered: (RING steps kept, RING entries).
fn rings(n: usize) -> (usize, usize, ktpu_l0::Package, ktpu_l0::Package) {
    let mut b = builder();
    for _ in 0..n {
        b.ring(1, 0, 1, true).unwrap();
    }
    let p = b.build(true, false);
    let low = engine::lower(&p, &f()).unwrap();
    let kept = low.steps.iter().filter(|s| s.op == Op::Ring).count();
    let ring = engine::il_code(engine::IL_RING);
    let entries = low
        .steps
        .iter()
        .filter(|s| s.op == Op::Engine)
        .flat_map(|s| engine::entries(&low, s))
        .filter(|e| e.0 == ring)
        .count();
    (kept, entries, p, low)
}

#[test]
fn rul_046_rings_past_the_queue_run_on_the_firmware_path() {
    // Four fit the interlink's queue: all engine entries.
    let (kept, entries, _, _) = rings(4);
    assert_eq!((kept, entries), (0, 4));
    // Five between waits: every RING stays a step, which waits for room.
    let (kept, entries, p, low) = rings(5);
    assert_eq!((kept, entries), (5, 0));
    replay::check(&p, &low, &f()).unwrap();
}

#[cfg(feature = "mutants")]
#[test]
fn rul_046_rings_lowered_into_the_engine_are_refused() {
    let (kept, entries, p, low) = ktpu_l0::mutant::with("l0.lower/rings_in_engine", || rings(5));
    assert_eq!((kept, entries), (0, 5));
    assert_eq!(replay::check(&p, &low, &f()).unwrap_err().clause, "RUL.046");
}
