//! docs/spec/stack/l0/binding.md

use super::*;
use ktpu_l0::WordOps;
use ktpu_l0::{apply_reloc, bind_addresses, engine, AddrField, Buffer, Kind, Op, Reloc, Step};

/// Bits [0, 40) of a word as its one address field.
fn low40(w: &Word, _: &str) -> Vec<AddrField> {
    vec![AddrField {
        segments: vec![(0, 40, 0)],
        value: w.get(0, 40),
    }]
}

/// A 40-bit address split 34 + 6: bits [0, 34) and [200, 206).
fn split(w: &Word, _: &str) -> Vec<AddrField> {
    vec![AddrField {
        segments: vec![(0, 34, 0), (200, 6, 34)],
        value: w.get(0, 34) | w.get(200, 6) << 34,
    }]
}

fn spans(list: &[(u64, u64)]) -> BTreeMap<u64, u64> {
    list.iter().copied().collect()
}

fn buf(default: u64) -> Buffer {
    Buffer {
        name: "b".into(),
        size: 64,
        default,
        kind: Kind::Temp,
        mesh: 0,
    }
}

#[test]
fn l0_bind_001_absolute_package_runs_as_written() {
    let p = pkg(vec![mg(1, 1)], vec![], vec![0x1234]);
    assert_eq!(p.bind(&[]).unwrap().payloads, p.payloads);
}

#[test]
fn l0_bind_002_field_in_span_becomes_relocation() {
    let mut b = builder();
    b.spans_from(&spans(&[(0x1000, 0x100)]));
    let i = b.payload(Word::from_u64(0x1010 | 0xAB << 48), Some(&low40), "MG");
    let j = b.payload(Word::from_u64(0x9000), Some(&low40), "MG");
    assert_eq!(b.payloads[i], Word::from_u64(0xAB << 48));
    assert_eq!(b.payloads[j], Word::from_u64(0x9000));
    assert_eq!(
        b.relocs,
        vec![Reloc {
            payload: 0,
            bit: 0,
            width: 40,
            buffer: 0,
            addend: 0x10,
            shift: 0
        }]
    );
}

#[test]
fn l0_bind_003_span_buffer_named_in_first_reached_order() {
    let mut b = builder();
    b.mesh = 2;
    b.spans_from(&spans(&[(0x1000, 0x100), (0x2000, 0x40)]));
    b.payload(Word::from_u64(0x2004), Some(&low40), "MG");
    b.payload(Word::from_u64(0x1000), Some(&low40), "MG");
    let names: Vec<&str> = b.buffers.iter().map(|x| x.name.as_str()).collect();
    assert_eq!(names, ["buf0", "buf1"]);
    assert_eq!((b.buffers[0].size, b.buffers[0].default), (0x40, 0x2000));
    assert_eq!(b.buffers[0].kind, Kind::Temp);
    assert_eq!(b.buffers[0].mesh, 2);
}

#[test]
fn l0_bind_004_split_field_one_relocation_per_segment() {
    let mut b = builder();
    let base = 0x14_0000_0000u64;
    b.spans_from(&spans(&[(base, 4096)]));
    let a = base + 0x40;
    let w = Word::from_u64(a & ((1 << 34) - 1)) | Word::placed(a >> 34, 200);
    b.payload(w, Some(&split), "MG");
    assert_eq!(b.payloads[0], Word::ZERO);
    let segs: Vec<(u64, u64, u64, i128)> = b
        .relocs
        .iter()
        .map(|r| (r.bit, r.width, r.shift, r.addend))
        .collect();
    assert_eq!(segs, [(0, 34, 0, 0x40), (200, 6, 34, 0x40)]);
    let bound = b.build(true, false).bind(&[]).unwrap();
    assert_eq!(bound.payloads[0], w);
}

#[test]
fn l0_bind_005_past_256_buffers_field_kept_absolute() {
    let mut b = builder();
    let list: Vec<(u64, u64)> = (0..257).map(|k| (0x1000 * (k + 1), 0x100)).collect();
    b.spans_from(&spans(&list));
    for &(base, _) in &list {
        b.payload(Word::from_u64(base + 8), Some(&low40), "MG");
    }
    assert_eq!(b.buffers.len(), 256);
    assert_eq!(b.relocs.len(), 256);
    assert_eq!(b.payloads[256], Word::from_u64(257 * 0x1000 + 8));
}

#[test]
fn l0_bind_006_bindings_are_span_bases_in_buffer_order() {
    let mut b = builder();
    b.spans_from(&spans(&[(0x1000, 64), (0x2000, 64)]));
    b.payload(Word::from_u64(0x2008), Some(&low40), "MG");
    b.payload(Word::from_u64(0x1010), Some(&low40), "MG");
    assert_eq!(b.bindings(), [0x2000, 0x1000]);
}

#[test]
fn l0_bind_032_no_defaults_build_writes_zero() {
    let mut b = builder();
    b.spans_from(&spans(&[(0x1000, 64)]));
    b.payload(Word::from_u64(0x1008), Some(&low40), "MG");
    assert_eq!(b.build(false, false).buffers[0].default, 0);
    assert_eq!(b.bindings(), [0x1000]);
}

#[test]
fn l0_bind_007_builds_at_two_addresses_are_byte_identical() {
    let f = f();
    let build = |base: u64| {
        let mut p = ktpu_l0::Program::new();
        p.send((1, 1), vec![Word::from_u64(base + 0x20)], vec![]);
        let s = spans(&[(base, 0x100)]);
        let opts = ktpu_l0::BuildOpts {
            addresses: Some(&low40),
            spans: Some(&s),
            ..Default::default()
        };
        let b = p.build(&f, opts).unwrap();
        (b.build(false, false).to_bytes(&f).unwrap(), b.bindings())
    };
    let (a, ba) = build(0x10_0000);
    let (c, bc) = build(0x77_0000);
    assert_eq!(a, c);
    assert_eq!((ba, bc), (vec![0x10_0000], vec![0x77_0000]));
}

#[test]
fn l0_bind_008_memory_steps_relocate_only_when_asked() {
    let mut b = builder();
    b.spans_from(&spans(&[(0x1000, 0x100)]));
    b.store(&[(0x1008, 5)], false, false);
    b.load(1, 0x1010, None, false, false);
    assert!(b.relocs.is_empty());
    b.store(&[(0x1008, 5)], false, true);
    b.load(1, 0x1010, None, false, true);
    b.load(1, 0x1010, None, true, true);
    let r: Vec<(u64, u64, i128)> = b
        .relocs
        .iter()
        .map(|r| (r.payload, r.bit, r.addend))
        .collect();
    assert_eq!(r, [(2, 0, 8), (3, 0, 0x10)]);
}

#[test]
fn l0_bind_009_nonzero_binding_wins() {
    assert_eq!(
        bind_addresses(&[buf(0x10), buf(0x20)], &[0x99]),
        [0x99, 0x20]
    );
}

#[test]
fn l0_bind_033_unbound_or_missing_binding_takes_default_rul_040() {
    let unbound = ktpu_l0::format::UNBOUND;
    assert_eq!(
        bind_addresses(&[buf(0x10), buf(0x20)], &[unbound, 0]),
        [0x10, 0]
    );
    assert_eq!(bind_addresses(&[buf(0x10)], &[]), [0x10]);
}

#[test]
fn l0_bind_010_reloc_sets_shifted_field_keeps_rest() {
    let r = Reloc {
        payload: 0,
        bit: 60,
        width: 8,
        buffer: 0,
        addend: 0x30,
        shift: 4,
    };
    let w = Word([u64::MAX, u64::MAX, 0, 0]);
    let got = apply_reloc(w, &r, 0x1200);
    let want = w.cleared(60, 8) | Word::placed(0x23, 60);
    assert_eq!(got, want);
    assert_eq!(got.get(60, 8), (0x1200 + 0x30) >> 4 & 0xFF);
}

#[cfg(feature = "mutants")]
#[test]
fn l0_bind_010_an_address_one_line_off_is_caught() {
    let r = Reloc {
        payload: 0,
        bit: 60,
        width: 8,
        buffer: 0,
        addend: 0x30,
        shift: 4,
    };
    let w = Word([u64::MAX, u64::MAX, 0, 0]);
    let got = ktpu_l0::mutant::with("l0.bind/addr_off", || apply_reloc(w, &r, 0x1200));
    assert_eq!(got.get(60, 8), (0x1200 + 0x40 + 0x30) >> 4 & 0xFF);
    assert_ne!(got, apply_reloc(w, &r, 0x1200));
}

#[test]
fn l0_bind_011_bind_applies_all_and_drops_relocations() {
    let mut p = pkg(
        vec![mg(1, 1)],
        vec![Step::new(Op::Dispatch, 0, 1, 0, 0)],
        vec![0xFF << 56],
    );
    p.buffers.push(buf(0x1000));
    p.relocs.push(Reloc {
        payload: 0,
        bit: 0,
        width: 40,
        buffer: 0,
        addend: 0x20,
        shift: 0,
    });
    let got = p.bind(&[0x5000]).unwrap();
    assert_eq!(got.payloads[0], Word::from_u64(0xFF << 56 | 0x5020));
    assert!(got.relocs.is_empty());
    assert_eq!(
        (got.units.clone(), got.steps.clone()),
        (p.units.clone(), p.steps.clone())
    );
}

#[test]
fn l0_bind_034_bound_package_has_no_buffers_keeps_header() {
    let mut p = pkg(vec![], vec![], vec![0]);
    p.buffers.push(buf(0x1000));
    p.buffer_names.push("b".into());
    p.signature = 9;
    p.ack_reserve = 2;
    p.checksum = true;
    let got = p.bind(&[]).unwrap();
    assert!(got.buffers.is_empty() && got.buffer_names.is_empty());
    assert_eq!((got.signature, got.ack_reserve, got.checksum), (9, 2, true));
}

#[test]
fn l0_bind_013_lower_refuses_relocations() {
    let mut p = pkg(vec![], vec![], vec![0]);
    p.buffers.push(buf(0x1000));
    p.relocs.push(Reloc {
        payload: 0,
        bit: 0,
        width: 8,
        buffer: 0,
        addend: 0,
        shift: 0,
    });
    assert_eq!(engine::lower(&p, &f()).unwrap_err().clause, "L0.DE.039");
}

#[test]
fn l0_bind_014_jumping_package_bound_then_unlowered() {
    let mut p = pkg(
        vec![mg(1, 1)],
        vec![Step::new(Op::Jump, 0, 0, 0, 0)],
        vec![0],
    );
    p.buffers.push(buf(0x1000));
    p.relocs.push(Reloc {
        payload: 0,
        bit: 0,
        width: 16,
        buffer: 0,
        addend: 4,
        shift: 0,
    });
    let bound = p.bind(&[]).unwrap();
    let low = engine::lower(&bound, &f()).unwrap();
    assert_eq!(low, bound);
    assert_eq!(low.payloads[0], Word::from_u64(0x1004));
}
