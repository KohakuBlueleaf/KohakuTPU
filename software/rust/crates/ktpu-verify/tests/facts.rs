//! Machine-variant witnesses: each machine fact the model reads, changed on
//! the machine, changes what the checker finds, by the amount the fact
//! says.

use ktpu_isa::unit::sru::config::{lookup, G};
use ktpu_isa::InstSet;
use ktpu_l0::{Op, Package, Step, Unit, Word};
use ktpu_machine::Machine;
use ktpu_verify::adapter::package::op;
use ktpu_verify::{check, Finding, Kind, Nodes, Target, U256};
use serde_json::json;

const MG: u64 = 0x4D47;
const VC: u64 = 0x5643;
const SR: u64 = 0x5352;

fn enc(set: &InstSet, name: &str, vals: &[(&str, i64)]) -> U256 {
    U256(set.by_name(name).unwrap().encode(vals).unwrap().0)
}

fn unit(type_code: u64, (x, y): (u64, u64)) -> Unit {
    Unit {
        type_code,
        x,
        y,
        mesh: 0,
        credit: 512,
        fetch: 0,
    }
}

fn step(op: u8, unit: u64, count: u64, arg: u64) -> Step {
    Step::new(Op::from_code(op as u64).unwrap(), unit, count, arg, 0)
}

/// Each unit's words dispatched in order, then `tail` (steps whose payload
/// indices count from the end of the dispatched words, and their payloads).
fn package(units: Vec<(Unit, Vec<U256>)>, tail: Vec<Step>, more: Vec<U256>) -> Package {
    let (mut steps, mut payloads) = (Vec::new(), Vec::new());
    let mut list = Vec::new();
    for (i, (u, words)) in units.into_iter().enumerate() {
        if !words.is_empty() {
            let at = payloads.len() as u64;
            steps.push(step(op::DISPATCH, i as u64, words.len() as u64, at));
            payloads.extend(words);
        }
        list.push(u);
    }
    let base = payloads.len() as u64;
    for mut s in tail {
        if s.op == Op::from_code(op::SEND as u64).unwrap() {
            s.arg += base;
        }
        steps.push(s);
    }
    payloads.extend(more);
    Package {
        units: list,
        steps,
        payloads: payloads.into_iter().map(|w| Word(w.0)).collect(),
        ..Default::default()
    }
}

/// Every finding for `p` as both runs of mesh 0 on `m`.
fn found(m: &Machine, p: &Package) -> Vec<Finding> {
    let t = Target::from_machine(m, Some(1)).unwrap();
    let runs: Vec<Nodes> = (0..2).map(|_| Nodes::from([(0, p.clone())])).collect();
    check(&runs, &t)
}

fn texts(found: &[Finding]) -> String {
    found
        .iter()
        .map(|f| format!("{} {}: {}", f.kind.name(), f.at, f.text))
        .collect::<Vec<_>>()
        .join("\n")
}

fn has(found: &[Finding], text: &str) -> bool {
    found.iter().any(|f| f.text.contains(text))
}

fn set(m: &Machine, path: &str, v: i64) -> Machine {
    m.with_fact(path, json!(v)).unwrap()
}

fn int(m: &Machine, path: &str) -> i64 {
    m.get(path).and_then(|v| v.as_i64()).unwrap()
}

// ------------------------------------------------------------- die.word_bytes
/// Cluster (1,1)'s plain DRAIN of one sub-tile to vector core (2,1)'s L1 at
/// byte address `addr`.
fn drain_to_core(t: &Target, addr: i64) -> Package {
    let d = enc(
        &t.isa.cluster.set,
        "DRAIN",
        &[
            ("n", 1),
            ("dnode", 1),
            ("dst_x", 2),
            ("dst_y", 1),
            ("addr", addr),
        ],
    );
    package(
        vec![(unit(MG, (1, 1)), vec![d]), (unit(VC, (2, 1)), vec![])],
        vec![],
        vec![],
    )
}

/// A peer drain's byte address is a word offset over `die.word_bytes`.
#[test]
fn drain_address_over_die_word_bytes() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let wb = int(&m, "die.word_bytes");
    // Past the core's L1 at either granule size.
    let addr = 3 * t.facts.vc_l1_words * wb;
    for wb in [wb, wb * 2] {
        let m = set(&m, "die.word_bytes", wb);
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let got = found(&m, &drain_to_core(&t, addr));
        let want = format!("words {}..", addr / wb);
        assert!(
            got.iter()
                .any(|f| f.kind == Kind::Bounds && f.text.contains(&want)),
            "word_bytes {wb}: no bounds finding at {want}:\n{}",
            texts(&got)
        );
    }
}

// ---------------------------------------------------------------- mg.acc_bits
/// A peer sub-tile is `mg.lanes`^2 elements of `mg.acc_bits`, in whole
/// granules: cluster (1,1)'s peer drain of one sub-tile past (1,0)'s
/// accumulator names that many granules.
#[test]
fn peer_subtile_granules_over_mg_acc_bits() {
    let m = Machine::named("sru_v9t7").unwrap();
    assert_eq!(
        (int(&m, "mg.lanes"), int(&m, "die.word_bytes")),
        (4, 32),
        "the granule counts below are for 16 elements and 256-bit granules"
    );
    let tiles = int(&m, "mg.tiles");
    // A multiple of every sub-tile size below (lcm 28), past every peer
    // buffer, inside the 16-bit offset.
    let off = (tiles * 7 / 28 + 1) * 28;
    assert!(off + 7 <= 0xFFFF);
    for (bits, sub) in [(int(&m, "mg.acc_bits"), 2), (64, 4), (8, 1), (100, 7)] {
        let m = set(&m, "mg.acc_bits", bits);
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let d = enc(
            &t.isa.cluster.set,
            "DRAIN",
            &[
                ("n", 1),
                ("dnode", 1),
                ("dst_x", 1),
                ("dst_y", 0),
                ("dbuf", 2),
                ("addr", off * 32),
            ],
        );
        let p = package(
            vec![(unit(MG, (1, 1)), vec![d]), (unit(MG, (1, 0)), vec![])],
            vec![],
            vec![],
        );
        let got = found(&m, &p);
        let want = format!("granules {off}..{} past buffer 2", off + sub - 1);
        assert!(
            has(&got, &want),
            "acc_bits {bits}: no {want}:\n{}",
            texts(&got)
        );
    }
}

// ---------------------------------------------------------------- mg.dq_limit
/// An emitting sweep of 2x2 sub-tiles whose drain never gets a credit: the
/// next POST waits for the drain queue to hold `mg.dq_limit` sub-tiles,
/// that is for 4 - limit of them to leave (none when 4 fit).
#[test]
fn array_waits_for_mg_dq_limit() {
    let m = Machine::named("sru_v9t7").unwrap();
    for limit in [int(&m, "mg.dq_limit"), 4, 3, 1] {
        let m = set(&m, "mg.dq_limit", limit);
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let set = &t.isa.cluster.set;
        let gemm = enc(
            set,
            "GEMM",
            &[
                ("gm", 2),
                ("gn", 2),
                ("emit", 1),
                ("dnode", 1),
                ("dst_x", 1),
                ("dst_y", 0),
                ("dbuf", 2),
                ("credit", 1),
            ],
        );
        let post = enc(set, "POST", &[("dst_x", 1), ("dst_y", 0), ("n", 0)]);
        let p = package(
            vec![
                (unit(MG, (1, 1)), vec![gemm, post]),
                (unit(MG, (1, 0)), vec![]),
            ],
            vec![],
            vec![],
        );
        let got = found(&m, &p);
        let held = has(&got, "the array waits for the drain queue");
        if limit < 4 {
            let want = format!(
                "the array waits for the drain queue: {} sub-tiles",
                4 - limit
            );
            assert!(
                has(&got, &want),
                "dq_limit {limit}: no {want}:\n{}",
                texts(&got)
            );
        } else {
            assert!(
                !held,
                "dq_limit {limit}: the array waited:\n{}",
                texts(&got)
            );
        }
    }
}

// ----------------------------------------------------------- sr.burst, sr.held
/// Row unit configuration words 0 and 1 for `nw` words a sub-row, `nsr`
/// sub-rows, the output as wide as the input (OUT 1).
fn sr_cfg(set: &InstSet, nw: i64, nsr: i64) -> Vec<U256> {
    let g = |name: &str, v: i64| {
        let (word, lo, _) = lookup(G, name).unwrap();
        (word, v << lo)
    };
    let mut cfg = [0i64; 2];
    for (w, v) in [g("NW", nw), g("OUT", 1), g("NSR", nsr)] {
        cfg[w as usize] |= v;
    }
    (0..2)
        .map(|w| enc(set, "CFG", &[("word", w as i64), ("value", cfg[w])]))
        .collect()
}

/// Row unit (2,1) holding all of one 16-word sub-row (A from memory) and 4
/// credits of its own, sending it on to row unit (2,2): it sends bursts of
/// `sr.burst` words while the credits cover one, so 4 words when a burst is
/// 4 or less, none when it is more.
#[test]
fn row_output_bursts_of_sr_burst() {
    let m = Machine::named("sru_v9t7").unwrap();
    for (burst, out) in [(int(&m, "sr.burst"), 0), (4, 4), (3, 3), (1, 4)] {
        let m = set(&m, "sr.burst", burst);
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let set = &t.isa.sru.set;
        let mut words = sr_cfg(set, 16, 1);
        words.push(enc(set, "POST", &[("x", 2), ("y", 1), ("n", 4)]));
        words.push(enc(
            set,
            "RUN",
            &[("amem", 1), ("peer", 1), ("px", 2), ("py", 2)],
        ));
        let p = package(
            vec![(unit(SR, (2, 1)), words), (unit(SR, (2, 2)), vec![])],
            vec![],
            vec![],
        );
        let got = found(&m, &p);
        let want = format!(
            "16 of 16 words in (0 landed), {out} out, credits {}",
            4 - out
        );
        assert!(
            has(&got, &want),
            "sr.burst {burst}: no {want}:\n{}",
            texts(&got)
        );
    }
}

/// Row unit (2,1) streaming 8 sub-rows of 16 words (all sent by the node)
/// to a row unit that never gives a credit: it takes `sr.held` sub-rows plus
/// its A FIFO (`sr.fifo_depth`) and no more.
#[test]
fn row_input_runs_sr_held_ahead() {
    let m = Machine::named("sru_v9t7").unwrap();
    let fifo = int(&m, "sr.fifo_depth");
    for held in [int(&m, "sr.held"), 1, 0, 3] {
        let m = set(&m, "sr.held", held);
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let set = &t.isa.sru.set;
        let mut words = sr_cfg(set, 16, 8);
        words.push(enc(
            set,
            "RUN",
            &[
                ("amem", 0),
                ("peer", 1),
                ("px", 2),
                ("py", 2),
                ("ax", 2),
                ("ay", 2),
                ("words", 128),
            ],
        ));
        // The node's CU_DATA burst: descriptor (COUNT, 128 words, buffer
        // 0 from 0), then the words.
        let mut d = U256::ZERO;
        d.set_bits(216, 8, 2);
        d.set_bits(224, 8, 127);
        let mut more = vec![d];
        more.extend((0..128).map(|_| U256::ZERO));
        let p = package(
            vec![(unit(SR, (2, 1)), words), (unit(SR, (2, 2)), vec![])],
            vec![step(op::SEND, 0, 129, 8 << 32)],
            more,
        );
        let got = found(&m, &p);
        let inn = held * 16 + fifo;
        let want = format!("{inn} of 128 words in ({} landed), 0 out", 128 - inn);
        assert!(
            has(&got, &want),
            "sr.held {held}: no {want}:\n{}",
            texts(&got)
        );
    }
}
