//! Hardware facts read from the machine, not constants: a machine variant
//! changes the output or is refused (the MXFP7 entry geometry and bytes, the
//! drain's L1 word address).

mod common;

use std::collections::BTreeMap;

use common::{machine, prog_steps};
use ktpu_l1::build;
use ktpu_l1::layout::buffer::Buffer;
use ktpu_l1::layout::tensor::{pack_a, Mx7};
use ktpu_l1::text::read;
use ktpu_machine::{Machine, UnitKind};
use serde_json::{json, Value};

fn variant(facts: &[(&str, u64)]) -> Machine {
    facts
        .iter()
        .fold(machine(), |m, (k, v)| m.with_fact(k, json!(v)).unwrap())
}

/// A `[4, 64]` operand of distinct FP16 values packed as one A tile.
fn packed(m: &Machine) -> Vec<u8> {
    let x: Vec<u16> = (0..256u16).map(|i| 0x3C00 + i * 3).collect();
    pack_a(&x, 4, 64, 1, 1, &Mx7::of(m).unwrap()).unwrap()
}

#[test]
fn mx7_entry_is_the_machines() {
    assert_eq!(
        Mx7::of(&machine()).unwrap(),
        Mx7 {
            lanes: 4,
            kblock: 32,
            words: 4,
            word_bytes: 32
        }
    );
}

#[test]
fn mx7_packing_follows_the_kblock() {
    // 64-value blocks: one entry of 8 words, not two of 4.
    let m = variant(&[("mg.kblock", 64), ("mg.entry_bits", 4 * (64 * 7 + 8))]);
    let f = Mx7::of(&m).unwrap();
    assert_eq!((f.kblock, f.words), (64, 8));
    let (v9, wide) = (packed(&machine()), packed(&m));
    assert_eq!((v9.len(), wide.len()), (256, 256));
    assert_ne!(v9, wide);
}

#[test]
fn mx7_facts_that_disagree_are_refused() {
    let m = variant(&[("mg.entry_bits", 900)]);
    assert_eq!(Mx7::of(&m).unwrap_err().clause, "L1.BODY.002");
}

/// An `mx7a[16, 64]` buffer's bytes on `m`.
fn mx7_bytes(m: &Machine) -> u64 {
    let text = "target ktpu.v9\nfn f.l1(a: mx7a[16, 64] @dram(gm=1, nk=1))\n    halt\n";
    let module = read(text, "<t>").unwrap();
    let t = &module.body("f", "l1").unwrap().params[0].1;
    Buffer::of(t).unwrap().bytes(m).unwrap()
}

/// One lane of 16 values in 120 bits: a 32-byte word an entry, two bytes a
/// value.
fn sparse_entry() -> Machine {
    variant(&[("mg.lanes", 1), ("mg.kblock", 16), ("mg.entry_bits", 120)])
}

#[test]
fn mx7_bytes_are_whole_entries() {
    assert_eq!(mx7_bytes(&machine()), 1024);
    assert_eq!(mx7_bytes(&sparse_entry()), 2048);
}

/// The drain's L1 address (`l1=3`) on `m`.
fn drain(m: &Machine) -> Value {
    let text = "target ktpu.v9\nfn f.l1(a: f16[4096] @dram)\n    send mg[0]\n        \
                drain n=2 to=mg[1] l1=3\n";
    let binds = BTreeMap::from([("a".to_string(), 4096)]);
    let got = build::programs(&read(text, "<t>").unwrap(), "f", &binds, m, None, None).unwrap();
    let kind = |c: (u32, u32)| {
        let mg = m.units(UnitKind::Mg).iter().any(|u| (u.0, u.1) == c);
        if mg { UnitKind::Mg.tag() } else { "?" }.to_string()
    };
    prog_steps(&kind, &got[0].program)[0][2][0].clone()
}

/// Each fact's mutant (the v9 constant back) breaks its variant witness.
#[cfg(feature = "mutants")]
#[test]
fn the_fact_mutants_break_their_witnesses() {
    use ktpu_l1::mutant::with;
    let m = variant(&[("mg.kblock", 64), ("mg.entry_bits", 4 * (64 * 7 + 8))]);
    let fixed = with("l1.facts/mx7-fixed", || (packed(&machine()), packed(&m)));
    assert_eq!(fixed.0, fixed.1);
    let wide = variant(&[("die.word_bytes", 64)]);
    let got = with("l1.facts/word-32", || drain(&wide));
    assert!(got.as_str().unwrap().contains("addr=0x60"), "{got}");
    assert_eq!(
        with("l1.facts/mx7-byte", || mx7_bytes(&sparse_entry())),
        1024
    );
}

#[test]
fn drain_address_is_in_machine_words() {
    let v9 = drain(&machine());
    assert!(v9.as_str().unwrap().contains("addr=0x60"), "{v9}");
    let wide = drain(&variant(&[("die.word_bytes", 64)]));
    assert!(wide.as_str().unwrap().contains("addr=0xc0"), "{wide}");
}
