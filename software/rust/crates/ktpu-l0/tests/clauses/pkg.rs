//! docs/spec/stack/l0/package.md

use super::*;
use ktpu_l0::format::{self, fnv1a, signature, type_code, unit_word};
use ktpu_l0::{checksum, Buffer, Kind, Op, Package, Reloc, Step};

/// The l0 golden `extra/loop_patch_fetch_memops`: its program, the bytes the
/// Python stack wrote for it, and the package it describes.
fn loop_patch_golden() -> (Vec<u8>, Package) {
    let path =
        crate::common::root().join("software/rust/golden/l0/extra/loop_patch_fetch_memops.json");
    let v: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let bytes = crate::common::from_hex_bytes(v["out"]["bytes"].as_str().unwrap());
    (bytes, crate::common::package(&v["out"]["package"]))
}

#[test]
fn l0_pkg_002_reader_gives_back_every_step_kind_field_by_field() {
    // Expectations written out from each program call (l0_dump.py) and the
    // step table, not from the writer.
    let (bytes, want) = loop_patch_golden();
    let got = Package::from_bytes(&bytes, &f()).unwrap();
    assert_eq!(got.steps, want.steps);
    assert_eq!(got.payloads, want.payloads);
    assert_eq!(got.units, want.units);
    let s = |i: usize| got.steps[i];
    let p = |i: u64| got.payloads[i as usize];
    let x = |op, unit, count, arg, flags| Step::new(op, unit, count, arg, flags);
    // load(1, 0x8000): r1 from payload 0.
    assert_eq!(s(0), x(Op::Load, 1, 0, 0, 0));
    assert_eq!(p(0), Word::from_u64(0x8000));
    // alu(2, MOV, 0, 0) with an immediate.
    assert_eq!(s(1), x(Op::Alu, 2, 0, 0, 2));
    // patch of the vc send's words from r0 (payload 4..7 are the 0x44s).
    assert_eq!(s(8), x(Op::Patch, 2, 4, 0x40808, 0));
    assert_eq!(p(4), Word::from_u64(0x44));
    // alu(2, ADD, 2, 1), then jump to "top" (step 2) while r2 <u r1.
    assert_eq!(s(15), x(Op::Alu, 2, 0x201, 1, 2));
    assert_eq!(s(16), x(Op::Jump, 2, 0x1203, 0, 0));
    // store of three pairs: two payload words from 13; store from registers.
    assert_eq!(s(21), x(Op::Store, 0, 2, 0xd, 0));
    let pair = "0x2000000000000500800000000000000010000000000005000";
    assert_eq!(p(13), Word::from_hex(pair).unwrap());
    assert_eq!(s(22), x(Op::Store, 0, 1, 0xf, 4));
    // ctrl([(0x1D0, 3)]): one pair at payload 16.
    assert_eq!(s(23), x(Op::Ctrl, 0, 1, 0x10, 0));
    // load(4, 0x38, ctrl) and load(5, 0x6000, index=r4).
    assert_eq!(s(24), x(Op::Load, 4, 0, 0x11, 0x10));
    assert_eq!(p(0x11), Word::from_u64(0x38));
    assert_eq!(s(25), x(Op::Load, 5, 4, 0x12, 8));
    assert_eq!(p(0x12), Word::from_u64(0x6000));
    // ring(1, 3, 2) fenced, ring(2, 0, 1) not: mesh, amount, slot | fence << 16.
    assert_eq!(s(26), x(Op::Ring, 1, 3, 0x1_0002, 0));
    assert_eq!(s(27), x(Op::Ring, 2, 0, 1, 0));
    // wait_slot(3, 2, consume).
    assert_eq!(s(29), x(Op::WaitSlot, 3, 2, 0, 0x20));
    // send_raw(mg2, kind 7, two words, ack): unit 2, words from payload 0x13.
    assert_eq!(s(32), x(Op::Send, 2, 2, 7 << 32 | 0x13, 0x80));
    assert_eq!(
        (p(0x13), p(0x14)),
        (Word::from_u64(0xABC), Word::from_u64(0xDEF))
    );
    // push(vc, 0x1234, 0x7000, 4, ack).
    assert_eq!(s(33), x(Op::Send, 1, 4, 8 << 32 | 0x15, 0xC0));
    assert_eq!(
        (p(0x15), p(0x16)),
        (Word::from_u64(0x1234), Word::from_u64(0x7000))
    );
    // recv(0x7800, 2, vc) and recv(0x7900, 1) from any unit.
    assert_eq!(s(34), x(Op::Recv, 1, 2, 0x17, 0));
    assert_eq!(s(35), x(Op::Recv, 0xFFFF, 1, 0x18, 0));
    assert_eq!(
        (p(0x17), p(0x18)),
        (Word::from_u64(0x7800), Word::from_u64(0x7900))
    );
}

#[cfg(feature = "mutants")]
#[test]
fn l0_pkg_002_a_reader_that_misreads_a_step_field_is_caught() {
    let (bytes, want) = loop_patch_golden();
    let got = ktpu_l0::mutant::with("l0.pkg/read_step_fields", || {
        // The writer is untouched: the same package gives the same bytes.
        assert_eq!(want.to_bytes(&f()).unwrap(), bytes);
        Package::from_bytes(&bytes, &f()).unwrap()
    });
    assert_ne!(got.steps, want.steps);
}

fn one_dispatch() -> Package {
    pkg(
        vec![with_port(mg(1, 1))],
        vec![Step::new(Op::Dispatch, 0, 2, 0, 0)],
        vec![0x1111, 0x2222],
    )
}

#[test]
fn l0_pkg_001_tables_in_order_aligned_to_32() {
    let mut p = one_dispatch();
    p.buffers.push(Buffer {
        name: "x".into(),
        size: 64,
        default: 0x1000,
        kind: Kind::Temp,
        mesh: 0,
    });
    p.relocs.push(Reloc {
        payload: 1,
        bit: 0,
        width: 8,
        buffer: 0,
        addend: 0,
        shift: 0,
    });
    let b = p.to_bytes(&f()).unwrap();
    // 96 header, unit 16 -> 32, buffer 32, step 16 -> 32, reloc 16 -> 32, 2 payloads 64.
    assert_eq!(b.len(), 96 + 32 + 32 + 32 + 32 + 64);
    let offs: Vec<u64> = (5..10).map(|k| q(&b, 8 * k)).collect();
    assert_eq!(offs, vec![96, 128, 160, 192, 224]);
    assert!(b[96 + 16..128].iter().all(|&x| x == 0));
}

#[test]
fn l0_pkg_002_reader_takes_offsets_from_header() {
    let f = f();
    let b = one_dispatch().to_bytes(&f).unwrap();
    // Payloads first, then steps, then units: offsets moved to match.
    let mut r = b[..96].to_vec();
    r.extend_from_slice(&b[160..224]);
    r.extend_from_slice(&b[128..160]);
    r.extend_from_slice(&b[96..128]);
    r[9 * 8..10 * 8].copy_from_slice(&96u64.to_le_bytes());
    r[7 * 8..8 * 8].copy_from_slice(&160u64.to_le_bytes());
    for k in [5usize, 6, 8] {
        r[k * 8..k * 8 + 8].copy_from_slice(&192u64.to_le_bytes());
    }
    let got = Package::from_bytes(&r, &f).unwrap();
    assert_eq!(got.units, one_dispatch().units);
    assert_eq!(got.steps, one_dispatch().steps);
    assert_eq!(got.payloads, one_dispatch().payloads);
}

#[test]
fn l0_pkg_003_word0_magic_version_header() {
    let b = one_dispatch().to_bytes(&f()).unwrap();
    assert_eq!(&b[0..4], b"KAPK");
    assert_eq!(q(&b, 0), 0x4B50_414B | 2 << 32 | 96 << 48);
}

#[test]
fn l0_pkg_004_signature_default_zero() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.await_(u, 1);
    let bytes = b.build(true, false).to_bytes(&f()).unwrap();
    assert_eq!(q(&bytes, 8), 0);
}

#[test]
fn l0_pkg_006_word2_total_and_flags() {
    let mut p = one_dispatch();
    p.checksum = true;
    let b = p.to_bytes(&f()).unwrap();
    assert_eq!(q(&b, 16), b.len() as u64 | 1 << 32);
}

#[test]
fn l0_pkg_007_rx_flag_exactly_with_recv() {
    let f = f();
    let mut p = one_dispatch();
    assert_eq!(q(&p.to_bytes(&f).unwrap(), 16) >> 32 & 2, 0);
    p.steps.push(Step::new(Op::Recv, 0xFFFF, 1, 0, 0));
    assert_eq!(q(&p.to_bytes(&f).unwrap(), 16) >> 32 & 2, 2);
}

#[test]
fn l0_pkg_008_counts() {
    let mut p = one_dispatch();
    p.ack_reserve = 7;
    p.steps.push(Step::bare(Op::Barrier));
    let b = p.to_bytes(&f()).unwrap();
    assert_eq!(q(&b, 24), 2);
    assert_eq!(q(&b, 32), 2 << 16 | 1 << 32 | 7 << 48);
}

#[test]
fn l0_pkg_009_checksum_word_zero_and_word11_zero() {
    let b = one_dispatch().to_bytes(&f()).unwrap();
    assert_eq!(q(&b, 80), 0);
    assert_eq!(q(&b, 88), 0);
}

#[test]
fn l0_pkg_010_checksum_fnv_and_mismatch_refused() {
    let f = f();
    let mut p = one_dispatch();
    p.checksum = true;
    let mut b = p.to_bytes(&f).unwrap();
    let mut z = b.clone();
    z[80..88].fill(0);
    assert_eq!(q(&b, 80), fnv1a(&z));
    assert_eq!(fnv1a(b""), 0xCBF2_9CE4_8422_2325);
    assert_eq!(fnv1a(b"a"), 0xAF63_DC4C_8601_EC8C);
    assert_eq!(checksum(&b), q(&b, 80));
    assert!(Package::from_bytes(&b, &f).is_ok());
    b[200] ^= 1;
    assert_eq!(
        Package::from_bytes(&b, &f).unwrap_err().clause,
        "L0.PKG.010"
    );
}

#[test]
fn l0_pkg_011_counts_past_16_bits_refused() {
    let p = Package {
        steps: vec![Step::bare(Op::Barrier); 0x10000],
        ..Package::default()
    };
    assert_eq!(p.to_bytes(&f()).unwrap_err().clause, "L0.PKG.011");
}

#[test]
fn l0_pkg_012_bad_magic_or_version_refused() {
    let f = f();
    let b = one_dispatch().to_bytes(&f).unwrap();
    let mut m = b.clone();
    m[0] ^= 1;
    assert_eq!(
        Package::from_bytes(&m, &f).unwrap_err().clause,
        "L0.PKG.012"
    );
    let mut v = b.clone();
    v[4] = 3;
    assert_eq!(
        Package::from_bytes(&v, &f).unwrap_err().clause,
        "L0.PKG.012"
    );
}

#[test]
fn l0_pkg_013_layout_refused() {
    let f = f();
    let b = one_dispatch().to_bytes(&f).unwrap();
    assert_eq!(
        Package::from_bytes(&b[..95], &f).unwrap_err().clause,
        "L0.PKG.013"
    );
    assert_eq!(
        Package::from_bytes(&b[..b.len() - 32], &f)
            .unwrap_err()
            .clause,
        "L0.PKG.013"
    );
    let mut t = b.clone();
    t[9 * 8..10 * 8].copy_from_slice(&(b.len() as u64 - 32).to_le_bytes());
    assert_eq!(
        Package::from_bytes(&t, &f).unwrap_err().clause,
        "L0.PKG.013"
    );
}

#[test]
fn l0_pkg_016_unit_table_words() {
    let mut u = with_port(mg(3, 2));
    u.mesh = 1;
    u.credit = 77;
    let b = pkg(vec![u], vec![], vec![]).to_bytes(&f()).unwrap();
    assert_eq!(q(&b, 96), 0x4D47 << 32 | 1 << 16 | 2 << 8 | 3);
    assert_eq!(q(&b, 104), 77 | (1 << 16 | 2 << 8) << 32);
    assert_eq!(unit_word(0x5643, 2, 1, 0), 0x5643_0000_0102);
}

#[test]
fn l0_pkg_017_type_codes() {
    assert_eq!(type_code("MG").unwrap(), 0x4D47);
    assert_eq!(type_code("VC").unwrap(), 0x5643);
    assert!(type_code("M").is_err());
    assert_eq!(format::type_name(0x5352), "SR");
}

#[test]
fn l0_pkg_018_unit_added_on_first_use_by_mesh_x_y() {
    let mut b = builder();
    assert_eq!(b.unit((2, 1), None, None).unwrap(), 0);
    assert_eq!(b.unit((1, 1), None, None).unwrap(), 1);
    assert_eq!(b.unit((2, 1), None, None).unwrap(), 0);
    assert_eq!(b.unit((2, 1), None, Some(1)).unwrap(), 2);
    assert_eq!(b.units.len(), 3);
    assert_eq!(b.units[0].type_code, 0x5643);
}

#[test]
fn l0_pkg_019_fetch_port_field() {
    let mut b = builder();
    b.fetch = Some((0, 2));
    let u = b.unit((1, 1), None, None).unwrap();
    assert_eq!(b.units[u].fetch, 1 << 16 | 2 << 8);
    let mut n = builder();
    let u = n.unit((1, 1), None, None).unwrap();
    assert_eq!(n.units[u].fetch, 0);
}

#[test]
fn l0_pkg_020_program_credit_is_inst_depth() {
    let f = f();
    let mut p = ktpu_l0::Program::new();
    p.send((1, 1), vec![Word::from_u64(1)], vec![]);
    let b = p.build(&f, Default::default()).unwrap();
    assert_eq!(b.units[0].credit, f.inst_depth);
    assert_eq!(b.units[0].credit, 512);
}

#[test]
fn l0_pkg_024_buffer_table_words() {
    let mut p = Package::default();
    p.buffers.push(Buffer {
        name: "w".into(),
        size: 4096,
        default: 0x40_0000,
        kind: Kind::Inout,
        mesh: 3,
    });
    let b = p.to_bytes(&f()).unwrap();
    let at = q(&b, 48) as usize;
    assert_eq!(q(&b, at), fnv1a(b"w"));
    assert_eq!(q(&b, at + 8), 4096);
    assert_eq!(q(&b, at + 16), 0x40_0000);
    assert_eq!(q(&b, at + 24), 2 | 3 << 8);
    let got = Package::from_bytes(&b, &f()).unwrap();
    assert_eq!(got.buffers[0].name, format!("#{:016x}", fnv1a(b"w")));
}

#[test]
fn l0_pkg_025_step_word() {
    let p = pkg(
        vec![],
        vec![Step::new(Op::Settle, 0x1_2345, 0x1_0000_0007, 0xFEED, 0x81)],
        vec![],
    );
    let b = p.to_bytes(&f()).unwrap();
    let at = q(&b, 56) as usize;
    assert_eq!(q(&b, at), 8 | 0x81 << 8 | 0x2345 << 16 | 7 << 32);
    assert_eq!(q(&b, at + 8), 0xFEED);
}

#[test]
fn l0_pkg_026_reloc_word_and_order() {
    let mut p = pkg(vec![], vec![], vec![0, 0]);
    p.buffers.push(Buffer {
        name: "b".into(),
        size: 64,
        default: 0,
        kind: Kind::Temp,
        mesh: 0,
    });
    let r = |payload, bit, addend| Reloc {
        payload,
        bit,
        width: 40,
        buffer: 0,
        addend,
        shift: 4,
    };
    p.relocs = vec![r(1, 0, -8), r(0, 100, 1), r(0, 4, 2)];
    let b = p.to_bytes(&f()).unwrap();
    let at = q(&b, 64) as usize;
    let w = |k: usize| q(&b, at + 16 * k);
    assert_eq!(w(0), 4 << 32 | 40 << 40 | 4 << 48);
    assert_eq!(q(&b, at + 8), 2);
    assert_eq!(w(1), 100 << 32 | 40 << 40 | 4 << 48);
    assert_eq!(w(2), 1 | 40 << 40 | 4 << 48);
    assert_eq!(q(&b, at + 40), (-8i64) as u64);
}

#[test]
fn l0_pkg_027_payload_least_significant_first() {
    let mut p = Package::default();
    p.payloads.push(Word([1, 2, 3, 4]));
    let b = p.to_bytes(&f()).unwrap();
    let at = q(&b, 72) as usize;
    assert_eq!(
        (0..4).map(|k| q(&b, at + 8 * k)).collect::<Vec<_>>(),
        [1, 2, 3, 4]
    );
}

#[test]
fn l0_pkg_031_signature_over_sorted_unit_words() {
    let a = [0x4D47_0000_0101u64, 0x5643_0000_0102];
    let mut bytes = Vec::new();
    for w in a {
        bytes.extend_from_slice(&w.to_le_bytes());
    }
    assert_eq!(signature(&[a[1], a[0]]), fnv1a(&bytes));
}

#[test]
fn l0_pkg_032_writer_refusals() {
    let f = f();
    let refused = |p: Package| p.to_bytes(&f).unwrap_err().clause;
    let base = || pkg(vec![mg(1, 1)], vec![], vec![0, 0]);
    let mut p = base();
    p.steps.push(Step::new(Op::Dispatch, 0, 3, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Await, 1, 1, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Fetch, 0, 1, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Alu, 16, 0, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Jump, 2, 0, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Engine, 0, 7, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Patch, 0, 2, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.steps.push(Step::new(Op::Recv, 3, 1, 0, 0));
    assert_eq!(refused(p), "L0.PKG.032");
    let mut p = base();
    p.relocs.push(Reloc {
        payload: 0,
        bit: 250,
        width: 8,
        buffer: 0,
        addend: 0,
        shift: 0,
    });
    assert_eq!(refused(p), "L0.PKG.032");
    let mut ok = base();
    ok.steps.push(Step::new(Op::Jump, 0, 0, 0, 0));
    ok.steps.push(Step::new(Op::Engine, 0, 6, 0, 0));
    ok.steps.push(Step::new(Op::Recv, 0xFFFF, 1, 1, 0));
    assert!(ok.to_bytes(&f).is_ok());
}

#[test]
fn l0_pkg_034_unknown_coordinate_refused() {
    let mut b = PackageBuilder::new(&f(), types(&[((1, 1), "MG")]), 32);
    assert_eq!(b.unit((5, 5), None, None).unwrap_err().clause, "L0.PKG.034");
    assert_eq!(b.unit((5, 5), Some("VC"), None).unwrap(), 0);
}
