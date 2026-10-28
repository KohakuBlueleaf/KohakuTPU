//! Parameter spans, literal memory and control offsets are distinct namespaces.

use std::collections::BTreeMap;

use ktpu_l0::{Facts, Package};
use ktpu_l1::{build, text};
use ktpu_machine::Machine;

fn compiled(source: &str, base: i128) -> (Package, Facts) {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let module = text::read(source, "<relocation witness>").unwrap();
    let programs = build::programs(
        &module,
        "f",
        &BTreeMap::from([("a".into(), base)]),
        &m,
        None,
        None,
    )
    .unwrap();
    assert_eq!(
        programs[0].program.spans,
        BTreeMap::from([(base as u64, 32)])
    );
    let builder = build::build(
        &programs[0],
        &m,
        build::Opts {
            relocate: true,
            ..Default::default()
        },
    )
    .unwrap();
    (builder.build(false, false), Facts::of(&m))
}

fn payload_bytes(p: &Package, f: &Facts) -> Vec<u8> {
    let bytes = p.to_bytes(f).unwrap();
    let at = u64::from_le_bytes(bytes[72..80].try_into().unwrap()) as usize;
    bytes[at..at + p.payloads.len() * 32].to_vec()
}

fn expected(words: &[[u64; 4]]) -> Vec<u8> {
    words
        .iter()
        .flatten()
        .flat_map(|x| x.to_le_bytes())
        .collect()
}

fn bound_parameter_witness() {
    let (p, f) = compiled(
        "target ktpu.v9\nfn f.l1(a: u64[4] @dram)\n    store a <- 5\n    store a + 8 <- 6\n",
        8192,
    );
    assert_eq!(p.buffers.len(), 1);
    assert_eq!(p.buffers[0].size, 32);
    assert_eq!(
        p.relocs
            .iter()
            .map(|r| (r.payload, r.addend))
            .collect::<Vec<_>>(),
        [(0, 0), (1, 8)]
    );
    for base in [0, 0x6000] {
        assert_eq!(
            payload_bytes(&p.bind(&[base]).unwrap(), &f),
            expected(&[[base, 5, u64::MAX, 0], [base + 8, 6, u64::MAX, 0]])
        );
    }
}

#[test]
fn bound_names_a_parameter_placement_not_literal_address_syntax() {
    bound_parameter_witness();
}

#[test]
fn controls_stay_fixed_even_inside_a_parameter_span() {
    let source = "target ktpu.v9\nfn f.l1(a: u64[4] @dram)\n    r1 = load a\n    r2 = ctrl xf_data\n    r3 = ctrl mvstat\n    ctrl xf_sel <- 0x104\n    store a + 8 <- 7\n";
    // The allocation deliberately covers xf_sel and xf_data's control offsets.
    let (p, f) = compiled(source, 0x1d0);
    assert_eq!(
        p.relocs
            .iter()
            .map(|r| (r.payload, r.addend))
            .collect::<Vec<_>>(),
        [(0, 0), (4, 8)]
    );
    for base in [0, 0x8000] {
        assert_eq!(
            payload_bytes(&p.bind(&[base]).unwrap(), &f),
            expected(&[
                [base, 0, 0, 0],
                [0x1d8, 0, 0, 0],
                [0x20, 0, 0, 0],
                [0x1d0, 0x104, u64::MAX, 0],
                [base + 8, 7, u64::MAX, 0]
            ])
        );
    }
}

#[test]
fn literal_memory_outside_parameter_spans_never_becomes_a_binding() {
    let (p, f) = compiled("target ktpu.v9\nfn f.l1(a: u64[4] @dram)\n    store 0x9000180000 <- 0xabcd\n    r1 = load 0x9000180000\n", 8192);
    assert!(p.buffers.is_empty());
    assert!(p.relocs.is_empty());
    assert_eq!(
        payload_bytes(&p.bind(&[]).unwrap(), &f),
        expected(&[[0x9000180000, 0xabcd, u64::MAX, 0], [0x9000180000, 0, 0, 0]])
    );
}

#[cfg(feature = "mutants")]
#[test]
fn dropped_memory_relocations_fail_the_independent_parameter_witness() {
    let failed = std::panic::catch_unwind(|| {
        ktpu_l0::mutant::with("l0.build/drop-address64", bound_parameter_witness)
    });
    assert!(failed.is_err());
}
