//! One test per clause of l1/program.md s15, on packages built here.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use ktpu_isa::InstSet;
use ktpu_l0::{Buffer, Op, Package, Reloc, Step, Unit, Word};
use ktpu_machine::Machine;
use ktpu_verify::adapter::package::{flag, op};
use ktpu_verify::{
    check, gate, key, schedule, Finding, GateError, Held, Kind, Mode, Nodes, Target, U256,
};

const MG: u64 = 0x4D47;
const VC: u64 = 0x5643;

fn target() -> Target {
    let m = Machine::named("multimesh_v9t6").unwrap();
    Target::from_machine(&m, Some(1)).unwrap()
}

fn unit(ty: u64, x: u64, y: u64, credit: u64) -> Unit {
    Unit {
        type_code: ty,
        x,
        y,
        mesh: 0,
        credit,
        fetch: 0,
    }
}

fn stepf(op: u8, unit: u64, count: u64, arg: u64, flags: u8) -> Step {
    Step::new(Op::from_code(op as u64).unwrap(), unit, count, arg, flags)
}

fn step(op: u8, unit: u64, count: u64, arg: u64) -> Step {
    stepf(op, unit, count, arg, 0)
}

fn pkg(units: Vec<Unit>, steps: Vec<Step>, payloads: Vec<U256>) -> Package {
    Package {
        units,
        steps,
        payloads: payloads.into_iter().map(|w| Word(w.0)).collect(),
        ..Default::default()
    }
}

fn enc(set: &InstSet, name: &str, vals: &[(&str, i64)]) -> U256 {
    U256(set.by_name(name).unwrap().encode(vals).unwrap().0)
}

fn core(name: &str, vals: &[(&str, i64)]) -> i64 {
    enc(&target().isa.vector.core.set, name, vals).low() as i64
}

fn cu(name: &str, vals: &[(&str, i64)]) -> U256 {
    enc(&target().isa.vector.set, name, vals)
}

fn imem(addr: i64, word: i64) -> U256 {
    cu("IMEM", &[("addr", addr), ("word", word)])
}

fn desc(ad: i64, fld: i64, value: i64) -> U256 {
    cu(
        "DESC",
        &[
            ("ad", ad),
            ("fld", fld),
            ("value", value & ((1 << 34) - 1)),
            ("value_hi", value >> 34),
        ],
    )
}

fn run(pc: i64) -> U256 {
    cu("RUN", &[("pc", pc)])
}

/// A vector core's program, loaded and run: IMEM words, descriptors, RUN.
fn program(words: &[i64], descs: &[(i64, i64, i64)]) -> Vec<U256> {
    let mut out: Vec<U256> = words
        .iter()
        .enumerate()
        .map(|(i, &w)| imem(i as i64, w))
        .collect();
    out.extend(descs.iter().map(|&(a, f, v)| desc(a, f, v)));
    out.push(run(0));
    out
}

/// The same package as both runs of mesh 0.
fn twice(p: &Package) -> Vec<Nodes> {
    (0..2).map(|_| Nodes::from([(0, p.clone())])).collect()
}

fn kinds(found: &[Finding]) -> Vec<Kind> {
    found.iter().map(|f| f.kind).collect()
}

/// A node awaiting a completion nothing will send.
fn stuck() -> Package {
    pkg(
        vec![unit(VC, 2, 1, 512)],
        vec![step(op::AWAIT, 0, 1, 0)],
        vec![],
    )
}

/// Cluster (1,1) at credit 1: a `fill recv` its second word waits behind,
/// fed by vector core (2,1)'s peer drain of one entry (4 words, COUNT).
fn fed() -> Package {
    let set = &target().isa.cluster.set;
    let mut payloads = vec![
        enc(set, "FILL", &[("recv", 1), ("n", 1)]),
        enc(set, "FILL", &[("recv", 1)]),
    ];
    let drain = core(
        "VDRAIN",
        &[("node", 1), ("ad", 0), ("dst_x", 1), ("dst_y", 1)],
    );
    payloads.extend(program(
        &[drain, core("VHALT", &[])],
        &[(0, 1, 4), (0, 0, 1 << 34)],
    ));
    pkg(
        vec![unit(MG, 1, 1, 1), unit(VC, 2, 1, 512)],
        vec![
            step(op::DISPATCH, 0, 2, 0),
            step(op::DISPATCH, 1, 5, 2),
            step(op::BARRIER, 0, 0, 0),
        ],
        payloads,
    )
}

#[test]
fn l1_ver_001_schedule_sends_past_a_blocked_item() {
    let t = target();
    let got = schedule(&Nodes::from([(0, fed())]), &Held::new(), &t).unwrap();
    assert_eq!(
        got.packages[&0].steps,
        vec![
            step(op::DISPATCH, 0, 1, 0),
            step(op::DISPATCH, 1, 5, 2),
            step(op::DISPATCH, 0, 1, 1),
            step(op::BARRIER, 0, 0, 0),
        ]
    );
    // The core's program is resident after.
    let held = &got.held[&0];
    assert_eq!(held.len(), 1);
    assert_eq!(held[0].coord, (2, 1));
    assert_eq!(held[0].imem.len(), 2);
}

#[test]
fn l1_ver_002_a_good_order_is_left_as_written() {
    let t = target();
    let once = schedule(&Nodes::from([(0, fed())]), &Held::new(), &t).unwrap();
    let again = schedule(&once.packages, &Held::new(), &t).unwrap();
    assert_eq!(again.packages, once.packages);
}

#[test]
fn l1_ver_003_key_names_no_absolute_address() {
    let t = target();
    let buf = Buffer {
        name: "b".into(),
        size: 64,
        default: 0,
        kind: ktpu_l0::Kind::Temp,
        mesh: 0,
    };
    let mut p = fed();
    p.buffers = vec![buf.clone(), buf];
    p.relocs = vec![Reloc {
        payload: 0,
        bit: 64,
        width: 40,
        shift: 0,
        buffer: 1,
        addend: 0,
    }];
    let k = |p: &Package, b: [u64; 2]| key(&BTreeMap::from([(0, (p.clone(), b.to_vec()))]), &t);
    let base = k(&p, [0x1000, 0x2000]);
    assert_eq!(base.len(), 32);
    assert_eq!(base, k(&p, [0x7000, 0x8000]), "moved, same key");
    assert_ne!(base, k(&p, [0x1000, 0x1020]), "overlapping buffers");
    let mut q = p.clone();
    q.steps.swap(0, 1);
    assert_ne!(base, k(&q, [0x1000, 0x2000]), "other steps");
}

#[test]
fn l1_ver_004_check_runs_both_runs_clean() {
    let t = target();
    assert_eq!(check(&twice(&fed()), &t), vec![]);
}

#[test]
fn l1_ver_005_gate_modes_and_cache() {
    let t = target();
    let runs = twice(&stuck());
    assert!(matches!("maybe".parse::<Mode>(), Err(GateError::Mode(_))));
    assert!("maybe"
        .parse::<Mode>()
        .unwrap_err()
        .to_string()
        .starts_with("L1.VER.005"));
    assert_eq!(gate(&runs, &t, Mode::Off, None), Ok(vec![]));
    let warn = gate(&runs, &t, Mode::Warn, None).unwrap();
    assert_eq!(kinds(&warn), vec![Kind::Deadlock, Kind::Deadlock]);
    match gate(&runs, &t, Mode::Refuse, None) {
        Err(GateError::Refused(f)) => assert_eq!(f, warn),
        other => panic!("not refused: {other:?}"),
    }
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join("gate");
    let _ = fs::remove_dir_all(&dir);
    assert_eq!(
        gate(&runs, &t, Mode::Warn, Some((&dir, "k1"))).unwrap(),
        warn
    );
    assert!(dir.join("k1.json").exists());
    // A stored entry is read back, not recomputed.
    fs::write(dir.join("k2.json"), r#"[["model","here","cached"]]"#).unwrap();
    let got = gate(&runs, &t, Mode::Refuse, Some((&dir, "k2"))).unwrap();
    assert_eq!(got, vec![Finding::new(Kind::Model, "here", "cached")]);
}

#[test]
fn l1_ver_006_deadlock() {
    let t = target();
    let found = check(&twice(&stuck()), &t);
    assert_eq!(
        found,
        vec![
            Finding::new(
                Kind::Deadlock,
                "node 0",
                "step 0 AWAIT u0: vc(0:2,1): 0 of 1 completions"
            ),
            Finding::new(
                Kind::Deadlock,
                "vc(0:2,1) CU",
                "waiting for words from the node"
            ),
        ]
    );
    assert_eq!(found[0].clause(), "L1.VER.006");
}

#[test]
fn l1_ver_007_rest_granules_never_waited_for() {
    let t = target();
    let mut d = U256::ZERO;
    d.set_bits(216, 8, 2); // COUNT
    let p = pkg(
        vec![unit(VC, 2, 1, 512)],
        vec![step(op::SEND, 0, 2, 8 << 32)],
        vec![d, U256::ZERO],
    );
    assert_eq!(
        check(&twice(&p), &t),
        vec![Finding::new(
            Kind::Rest,
            "vc(0:2,1)",
            "2 granules landed and never waited for"
        )]
    );
}

#[test]
fn l1_ver_008_port_bursts_from_two_senders_unordered() {
    let t = target();
    let drain = core(
        "VDRAIN",
        &[("node", 1), ("ad", 0), ("dst_x", 1), ("dst_y", 1)],
    );
    let mut payloads = program(&[drain, core("VHALT", &[])], &[]);
    payloads.extend(payloads.clone());
    let p = pkg(
        vec![unit(VC, 2, 1, 512), unit(VC, 2, 2, 512)],
        vec![step(op::DISPATCH, 0, 3, 0), step(op::DISPATCH, 1, 3, 3)],
        payloads,
    );
    let found = check(&twice(&p), &t);
    assert!(!found.is_empty() && found.iter().all(|f| f.kind == Kind::Port));
    assert_eq!(found[0].at, "mg(0:1,1)");
    assert_eq!(
        found[0].text,
        "a burst from vc(0:2,2) D engine is not ordered after the one from vc(0:2,1) D engine: their flits can interleave"
    );
}

/// A fill and an unpack of the same L1 word, ordered or not by a sync.
fn fill_unpack(sync: bool) -> Package {
    let mut words = vec![core("VFILL", &[("ad", 0)])];
    if sync {
        words.push(core("VSYNC", &[("waiter", 1)]));
    }
    words.push(core("VUNPK", &[("reg", 0), ("n1", 0), ("off", 0)]));
    words.push(core("VHALT", &[]));
    let payloads = program(&words, &[]);
    let n = payloads.len() as u64;
    pkg(
        vec![unit(VC, 2, 1, 512)],
        vec![step(op::DISPATCH, 0, n, 0)],
        payloads,
    )
}

#[test]
fn l1_ver_009_race_unordered_l1_accesses() {
    let t = target();
    assert_eq!(
        check(&twice(&fill_unpack(false)), &t),
        vec![Finding::new(
            Kind::Race,
            "vc(0:2,1)",
            "L1 0: VFILL at pc 0 and VUNPK at pc 1 are not ordered"
        )]
    );
    assert_eq!(check(&twice(&fill_unpack(true)), &t), vec![]);
}

#[test]
fn l1_ver_010_bounds_a_buffer_the_core_lacks() {
    let t = target();
    let mut d = U256::ZERO;
    d.set_bits(248, 8, 1);
    let p = pkg(
        vec![unit(VC, 2, 1, 512)],
        vec![step(op::SEND, 0, 2, 8 << 32)],
        vec![d, U256::ZERO],
    );
    let found = check(&twice(&p), &t);
    let want = Finding::new(
        Kind::Bounds,
        "node 0",
        "burst to vc(0:2,1) rejected: buffer 1: a vector core has only buffer 0",
    );
    assert_eq!(found, vec![want.clone(), want]);
}

#[test]
fn l1_ver_011_route_a_coordinate_with_no_unit() {
    let t = target();
    let p = pkg(
        vec![unit(MG, 5, 5, 512)],
        vec![step(op::BARRIER, 0, 0, 0)],
        vec![],
    );
    assert_eq!(
        check(&twice(&p), &t),
        vec![Finding::new(
            Kind::Route,
            "node 0",
            "the package names (5,5), where no unit is"
        )]
    );
}

#[test]
fn l1_ver_012_loop_on_data_runs_three_trips() {
    let t = target();
    let p = pkg(
        vec![],
        vec![step(op::LOAD, 0, 0, 0), stepf(op::JUMP, 0, 2, 0, flag::IMM)],
        vec![U256::ZERO],
    );
    let found = check(&twice(&p), &t);
    assert_eq!(
        found,
        vec![
            Finding::new(
                Kind::Loop,
                "node 0",
                "step 1: a loop on data, checked for 0..3 trips"
            ),
            Finding::new(
                Kind::Loop,
                "node 0",
                "step 4: a loop on data, checked for 0..3 trips"
            ),
        ]
    );
    assert!(gate(&twice(&p), &t, Mode::Refuse, None).is_ok());
}

#[test]
fn l1_ver_019_vector_cu_word_no_format_takes() {
    let t = target();
    let p = pkg(
        vec![unit(VC, 2, 1, 512)],
        vec![step(op::DISPATCH, 0, 1, 0)],
        vec![U256::ZERO],
    );
    let found = check(&twice(&p), &t);
    let want = Finding::new(Kind::Vector, "vc(0:2,1) CU", "no vector CU instruction");
    assert_eq!(found, vec![want]);
    assert!(gate(&twice(&p), &t, Mode::Refuse, None).is_err());
}

#[test]
fn l1_ver_015_refusing_kinds() {
    let refusing: Vec<&str> = Kind::ALL
        .into_iter()
        .filter(|k| k.refuses())
        .map(|k| k.name())
        .collect();
    assert_eq!(
        refusing,
        ["deadlock", "rest", "port", "race", "bounds", "route", "line", "cluster", "vector"]
    );
}

#[test]
fn l1_ver_016_no_order_completes() {
    let t = target();
    let e = schedule(&Nodes::from([(0, stuck())]), &Held::new(), &t).unwrap_err();
    assert_eq!(kinds(&e.findings), vec![Kind::Deadlock, Kind::Deadlock]);
    assert!(e.to_string().starts_with("L1.VER.016"));
}
