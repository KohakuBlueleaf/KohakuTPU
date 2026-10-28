//! Unbound framework packages bind without changing non-address payload bits.

#![cfg(feature = "stack")]

use ktpu::{compile, Cache, CardStack, Input};
use ktpu_l0::{Facts, Op, Package};
use ktpu_machine::Machine;

fn memops() -> Result<(), String> {
    let m = Machine::named("vlt_v9t6_2n").map_err(|e| e.to_string())?;
    let source =
        include_str!("../../../../language/kohakutpu/language/examples/l1/node/memops.ktpu");
    let c = compile(
        &CardStack::default(),
        &Input::L1(source.into()),
        Some("memops"),
        &m,
        &Cache::off(),
    )
    .map_err(|e| e.to_string())?;
    if let Some(why) = c.bound {
        return Err(why);
    }
    let out = c.packages.get(&0).ok_or("no unbound package")?;
    assert_eq!(out.params, vec!["out"]);
    let f = Facts::of(&m);
    let p = Package::from_bytes(&out.bytes, &f).map_err(|e| e.to_string())?;
    if p.relocs.len() != 6 || p.buffers.len() != 1 || p.buffers[0].default != 0 {
        return Err(format!("missing memory fields: {:?}", p.relocs));
    }
    // L1 source order and L0 pair layout, independent of relocation records.
    let fields = [
        (Op::Store, 0, Some(0x1234)),
        (Op::Load, 0, None),
        (Op::Store, 8, Some(1)),
        (Op::Store, 16, Some(2)),
        (Op::Recv, 32, None),
        (Op::Store, 64, Some(3)),
    ];
    let memory: Vec<_> = p
        .steps
        .iter()
        .filter(|s| matches!(s.op, Op::Store | Op::Recv) || (s.op == Op::Load && s.flags & 16 == 0))
        .collect();
    assert_eq!(memory.len(), fields.len());
    for base in [0x0020_0000_u64, 0x1234_5600] {
        let bound = p.bind(&[base]).map_err(|e| e.to_string())?;
        let bytes = bound.to_bytes(&f).map_err(|e| e.to_string())?;
        let payload_at = u64::from_le_bytes(bytes[72..80].try_into().unwrap()) as usize;
        for (step, &(op, offset, value)) in memory.iter().zip(&fields) {
            assert_eq!(step.op, op);
            let limbs = if let Some(value) = value {
                [base + offset, value, u64::MAX, 0]
            } else {
                [base + offset, 0, 0, 0]
            };
            let expected: Vec<u8> = limbs.into_iter().flat_map(u64::to_le_bytes).collect();
            let at = payload_at + step.arg as usize * 32;
            if bytes[at..at + 32] != expected {
                return Err(format!(
                    "{op:?} at {base:#x}+{offset}: bytes {:?}, expected {expected:?}",
                    &bytes[at..at + 32]
                ));
            }
        }
        assert!(bound.relocs.is_empty() && bound.buffers.is_empty());
    }
    Ok(())
}

#[test]
fn unbound_memops_has_every_buffer_field_and_two_independent_byte_layouts() {
    memops().unwrap();
}

#[cfg(feature = "mutants")]
#[test]
fn dropping_address64_is_killed_by_the_memops_witness() {
    let result = ktpu::mutant::with("l0.build/drop-address64", memops).unwrap();
    assert!(
        result.is_err(),
        "dropping address64 escaped the byte witness"
    );
}

#[cfg(feature = "mutants")]
#[test]
fn clobber_is_killed_by_the_memops_byte_witness() {
    let result = ktpu::mutant::with("l0.bind/clobber", memops).unwrap();
    assert!(result.is_err(), "clobber escaped the byte witness");
}
