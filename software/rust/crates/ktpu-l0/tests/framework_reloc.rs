//! Framework memory fields share payload relocation; control offsets do not.

#[path = "common/mod.rs"]
mod common;

use std::collections::BTreeMap;

use ktpu_l0::build::PackageBuilder;
use ktpu_l0::format::{ANY_UNIT, F_MEM};
use ktpu_l0::{Op, Word};

#[test]
fn all_framework_memory_fields_use_the_shared_span_rule() {
    let f = common::v9t6();
    let mut b = PackageBuilder::new(&f, f.unit_types(), f.inst_depth);
    b.fetch = f.fetch_port;
    b.spans_from(&BTreeMap::from([(0x1000, 256)]));
    let unit = b.unit(f.units["MG"][0], None, None).unwrap();
    b.store(&[(0x1000, 0xaabb), (0x1008, 0xccdd)], false, true);
    b.load(1, 0x1010, Some(2), false, true);
    b.push(
        unit,
        Word([0x1122, 0x3344, 0x5566, 0x7788]),
        0x1020,
        1,
        true,
        true,
    );
    b.recv(0x1040, 1, ANY_UNIT, true);
    b.fetch_from(unit, 0x1060, 1).unwrap();
    // Typed control offsets are not allocation addresses, even when numeric ranges overlap.
    b.ctrl(&[(0x1000, 0x1234)], false);
    b.load(2, 0x1000, None, true, true);
    let p = b.build(false, false);
    assert_eq!(p.relocs.len(), 6);
    assert_eq!(p.buffers.len(), 1);
    assert_eq!(
        p.relocs
            .iter()
            .map(|r| (r.payload, r.bit, r.width, r.addend))
            .collect::<Vec<_>>(),
        vec![
            (0, 0, 64, 0),
            (0, 128, 64, 8),
            (1, 0, 64, 16),
            (3, 0, 64, 32),
            (4, 0, 64, 64),
            (5, 0, 64, 96)
        ]
    );
    for base in [0, 0x1234_0000_u64] {
        let bound = p.bind(&[base]).unwrap();
        let expected = [
            [base, 0xaabb, base + 8, 0xccdd],
            [base + 16, 0, 0, 0],
            [0x1122, 0x3344, 0x5566, 0x7788],
            [base + 32, 0, 0, 0],
            [base + 64, 0, 0, 0],
            [base + 96, 0, 0, 0],
            [0x1000, 0x1234, u64::MAX, 0],
            [0x1000, 0, 0, 0],
        ];
        let bytes = bound.to_bytes(&f).unwrap();
        let payload_at = u64::from_le_bytes(bytes[72..80].try_into().unwrap()) as usize;
        let wanted: Vec<u8> = expected
            .into_iter()
            .flatten()
            .flat_map(u64::to_le_bytes)
            .collect();
        assert_eq!(&bytes[payload_at..payload_at + wanted.len()], wanted);
        let fetch = bound.steps.iter().find(|s| s.op == Op::Fetch).unwrap();
        assert_eq!(fetch.arg, base + 96);
        assert_eq!(fetch.flags & F_MEM, 0);
        let step_at = u64::from_le_bytes(bytes[56..64].try_into().unwrap()) as usize;
        assert_eq!(
            &bytes[step_at + 4 * 16 + 8..step_at + 5 * 16],
            (base + 96).to_le_bytes()
        );
    }
    assert_eq!(p.steps[4].flags & F_MEM, F_MEM);
}

#[test]
fn fetch_checks_full_host_index_and_preserves_runtime_step_patches() {
    let f = common::v9t6();
    let mut b = PackageBuilder::new(&f, f.unit_types(), f.inst_depth);
    b.fetch = f.fetch_port;
    b.spans_from(&BTreeMap::from([(0x1000, 256)]));
    let unit = b.unit(f.units["MG"][0], None, None).unwrap();
    b.patch(1, 1, 64, 40, 0, true, false).unwrap();
    b.fetch_from(unit, 0x1020, 1).unwrap();
    let p = b.build(false, false);
    let bound = p.bind(&[0x2000]).unwrap();
    assert_eq!(bound.steps[0], p.steps[0]);
    assert_eq!(bound.steps[1].arg, 0x2020);
    assert_eq!(bound.steps[1].flags & F_MEM, 0);
    let mut malformed = p.clone();
    malformed.steps[1].arg |= 1 << 32;
    assert_eq!(malformed.check(&f).unwrap_err().clause, "L0.PKG.032");
    assert_eq!(malformed.bind(&[0x2000]).unwrap_err().clause, "L0.PKG.032");
    let mut payload_patch = p.clone();
    payload_patch.steps[0].arg &= !(1 << 24);
    payload_patch.steps[0].count = p.steps[1].arg;
    assert_eq!(
        payload_patch.bind(&[0x2000]).unwrap_err().clause,
        "L0.STEP.054"
    );
}

#[test]
fn literal_memory_without_spans_and_empty_fetch_retain_their_encoding() {
    let f = common::v9t6();
    let mut b = PackageBuilder::new(&f, f.unit_types(), f.inst_depth);
    b.fetch = f.fetch_port;
    let unit = b.unit(f.units["MG"][0], None, None).unwrap();
    b.store(&[(0x1234, 99)], false, true);
    b.fetch_from(unit, 0x5000, 0).unwrap();
    b.fetch_from(unit, 0x5000, 1).unwrap();
    assert!(b.relocs.is_empty());
    assert_eq!(b.steps.len(), 2);
    assert_eq!(b.steps[1].arg, 0x5000);
    assert_eq!(b.steps[1].flags, 0);
}
