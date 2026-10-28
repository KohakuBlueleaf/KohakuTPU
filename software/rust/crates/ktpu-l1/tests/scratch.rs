//! Scratch arenas (`layout::scratch`, `build::programs_in`): a body with
//! more scratch parameters than a package has relocation buffers (256,
//! L0.BIND.005) builds relocatable, its package bound at shifted addresses
//! equal to the package built at them; with one span a parameter it is not.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_l0::Package;
use ktpu_l1::build::{programs, programs_in, Builder, Opts};
use ktpu_l1::layout::scratch::{arenas, Arena};
use ktpu_l1::lower::node::NodeProgram;
use ktpu_l1::text::{read, Module};
use ktpu_machine::Machine;

const SCRATCH: usize = 300;
const CALLER_AT: i128 = 0x0100_0000;
const ARENA_AT: u64 = 0x0200_0000;
const SHIFT: u64 = 256;

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

/// `fn f.l1(a, h0..h{SCRATCH})`: a word of `a` copied into each `hK`.
fn module() -> Module {
    let params: Vec<String> = (0..SCRATCH)
        .map(|k| format!("h{k}: f16[16] @dram"))
        .collect();
    let copies: String = (0..SCRATCH)
        .map(|k| format!("        copy w(a, (1, 32)) -> w(h{k}, (1, 32))\n"))
        .collect();
    let text = format!(
        "target ktpu.v9\nfn f.l1(a: f16[4096] @dram, {})\n    move\n{copies}",
        params.join(", ")
    );
    read(&text, "<scratch>").unwrap()
}

/// The single node's package, relocatable, and its bindings.
fn package(progs: &[NodeProgram], m: &Machine) -> (Package, Vec<u64>) {
    assert_eq!(progs.len(), 1);
    let b = Builder::new(m)
        .unwrap()
        .build(
            &progs[0],
            Opts {
                relocate: true,
                ..Opts::default()
            },
        )
        .unwrap();
    (b.build(false, false), b.bindings())
}

/// Whether the package built at `at(0)` and bound at the bindings of the
/// one built at `at(SHIFT)` equals that one bound at its own.
fn relocatable(at: impl Fn(u64) -> Vec<NodeProgram>, m: &Machine) -> bool {
    let (p0, _) = package(&at(0), m);
    let (p1, b1) = package(&at(SHIFT), m);
    p0.bind(&b1).unwrap() == p1.bind(&b1).unwrap()
}

#[test]
fn scratch_params_share_one_arena() {
    let (mo, m) = (module(), machine());
    let caller: BTreeSet<String> = ["a".to_string()].into();
    let got = arenas(&mo, "f", &caller, &m).unwrap();
    let line = m.card.dram.line_bytes as u64;
    let members: Vec<(String, u64)> = (0..SCRATCH)
        .map(|k| (format!("h{k}"), k as u64 * line))
        .collect();
    assert_eq!(
        got,
        vec![Arena {
            space: "dram".into(),
            node: None,
            bytes: SCRATCH as u64 * line,
            members,
        }]
    );
    assert_eq!(got[0].name(), "scratch:dram");
}

/// Independent offset and extent checks, including the card's gather shape.
fn line_isolated(m: &Machine) -> bool {
    let caller = BTreeSet::from(["a".to_string()]);
    [1, 16, 17, 784].into_iter().all(|elements| {
        let params = (0..2177)
            .map(|k| format!("h{k}: f16[{elements}] @dram"))
            .collect::<Vec<_>>()
            .join(", ");
        let mo = read(
            &format!("target ktpu.v9\nfn f.l1(a: f16[16] @dram, {params})\n    move\n"),
            "<line>",
        )
        .unwrap();
        let ar = arenas(&mo, "f", &caller, m).unwrap();
        let line = u64::from(m.card.dram.line_bytes);
        let bytes = 2 * elements;
        ar.len() == 1
            && ar[0].members.len() == 2177
            && ar[0].members.iter().all(|(_, off)| off % line == 0)
            && ar[0].members.windows(2).all(|w| w[0].1 + bytes <= w[1].1)
            && ar[0].bytes.is_multiple_of(line)
            && ar[0].bytes >= ar[0].members.last().unwrap().1 + bytes
    })
}

#[test]
fn every_scratch_member_owns_whole_write_lines() {
    let m = machine();
    assert_eq!((m.card.dram.line_bytes, m.die.word_bytes), (64, 32));
    assert!(line_isolated(&m));
    let wider = m.with_fact("card.dram.line_bytes", 128.into()).unwrap();
    assert!(line_isolated(&wider));
}

#[cfg(feature = "mutants")]
#[test]
fn word_aligned_scratch_mutant_is_killed() {
    assert!(!ktpu_l1::mutant::with(
        "l1.layout/word-aligned-arena",
        || { line_isolated(&machine()) }
    ));
}

#[test]
fn stage_and_node_arenas_isolate_members_and_tails() {
    let m = machine();
    let mo = read(
        "target ktpu.v9\nfn f.l1(a: f16[16] @dram, d0: u8[33] @dram, s0: u8[1] @stage(node=0), s1: u8[33] @stage(node=1), d1: u8[1] @dram, s2: u8[33] @stage(node=0), s3: u8[1] @stage(node=1))\n    move\n",
        "<spaces>",
    ).unwrap();
    let ar = arenas(&mo, "f", &BTreeSet::from(["a".into()]), &m).unwrap();
    assert_eq!(ar.len(), 3);
    for a in ar {
        assert_eq!(a.members.len(), 2);
        assert_eq!(a.members[0].1, 0);
        assert_eq!(a.members[1].1, 64);
        assert_eq!(a.bytes, 128);
    }
}

#[test]
fn an_arena_package_is_relocatable() {
    let (mo, m) = (module(), machine());
    let caller: BTreeSet<String> = ["a".to_string()].into();
    let ar = arenas(&mo, "f", &caller, &m).unwrap();
    let at = |shift: u64| {
        let binds = BTreeMap::from([("a".to_string(), CALLER_AT + shift as i128)]);
        let bases = vec![(ar[0].clone(), ARENA_AT + shift)];
        programs_in(&mo, "f", &binds, &bases, &m, None, None).unwrap()
    };
    assert_eq!(package(&at(0), &m).1.len(), 2, "a and the arena");
    assert!(relocatable(at, &m));
}

#[test]
fn a_span_per_scratch_param_is_not() {
    let (mo, m) = (module(), machine());
    let at = |shift: u64| {
        let mut binds = BTreeMap::from([("a".to_string(), CALLER_AT + shift as i128)]);
        for k in 0..SCRATCH {
            binds.insert(
                format!("h{k}"),
                (ARENA_AT + shift + 4096 * k as u64) as i128,
            );
        }
        programs(&mo, "f", &binds, &m, None, None).unwrap()
    };
    assert!(!relocatable(at, &m));
}
