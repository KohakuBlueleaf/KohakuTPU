//! `docs/spec/stack/isa/sru.md`.

use super::{at, isa, machine, refused};
use ktpu_isa::unit::sru::config::{lookup, F, G, O, P};
use ktpu_isa::unit::sru::{e8, schedule, words, Job, OpValue, Pool, PoolOp};
use ktpu_isa::{Coord, Machine, SruInst};
use std::collections::BTreeMap;

fn sru_machine() -> Machine {
    Machine::named("sru_v9t7").unwrap()
}

fn op(fields: &[(&str, OpValue)]) -> PoolOp {
    fields
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

fn name(s: &str) -> OpValue {
    OpValue::Name(s.into())
}

fn tap(k: usize) -> OpValue {
    OpValue::Tap("H".into(), k)
}

#[test]
fn isa_sr_001_only_on_a_machine_with_sr_units() {
    refused(Pool::of(&machine()), "ISA.SR.001");
    let pool = Pool::of(&sru_machine()).unwrap();
    assert_eq!(
        (pool.f_lanes, pool.h_taps, pool.ldep, pool.wlead, pool.taps),
        (16, 2, 18, 2, 32)
    );
}

#[test]
fn isa_sr_010_cfg() {
    let s = isa().sru;
    at(&s.cfg, "op", 255, 252);
    at(&s.cfg, "word", 251, 244);
    at(&s.cfg, "value", 31, 0);
    let w = SruInst::Config {
        words: vec![(0x40, 0xDEAD_BEEF)],
    }
    .encode(&s)
    .unwrap();
    assert_eq!(
        (w[0].get(252, 4), w[0].get(244, 8), w[0].get(0, 32)),
        (1, 0x40, 0xDEAD_BEEF)
    );
    refused(
        SruInst::Config {
            words: vec![(256, 0)],
        }
        .encode(&s),
        "ISA.SR.010",
    );
}

#[test]
fn isa_sr_011_load() {
    let s = isa().sru;
    for (n, hi, lo) in [
        ("what", 251, 250),
        ("first", 249, 240),
        ("addr", 239, 200),
        ("words", 199, 188),
    ] {
        at(&s.load, n, hi, lo);
    }
    let w = SruInst::Load {
        what: 2,
        addr: 0x1000,
        words: 4,
        first: 3,
    }
    .encode(&s)
    .unwrap();
    assert_eq!(w[0].get(252, 4), 2);
    assert_eq!(s.addresses(w[0])[0].value, 0x1000);
    refused(
        SruInst::Load {
            what: 4,
            addr: 0,
            words: 1,
            first: 0,
        }
        .encode(&s),
        "ISA.SR.011",
    );
}

#[test]
fn isa_sr_012_run() {
    let s = isa().sru;
    let want = [
        ("amem", 251, 251),
        ("peer", 250, 250),
        ("aaddr", 249, 210),
        ("astride", 209, 194),
        ("oaddr", 193, 154),
        ("ostride", 153, 138),
        ("baddr", 137, 98),
        ("bstride", 97, 82),
        ("px", 81, 78),
        ("py", 77, 74),
        ("pbuf", 73, 70),
        ("poff", 69, 54),
        ("count", 53, 53),
        ("ax", 52, 49),
        ("ay", 48, 45),
        ("words", 44, 21),
    ];
    for (n, hi, lo) in want {
        at(&s.run, n, hi, lo);
    }
    let stream = SruInst::Run {
        a: None,
        astride: 0,
        out: 0x9000,
        ostride: 0,
        b: 0,
        bstride: 0,
        src: Coord(2, 1),
        to: Some(Coord(1, 3)),
        buf: 1,
        at: 128,
        count: true,
        words: 64,
    };
    let w = stream.encode(&s).unwrap()[0];
    assert_eq!(
        (w.get(251, 1), w.get(250, 1), w.get(154, 40), w.get(21, 24)),
        (0, 1, 0, 64)
    );
    assert!(s.addresses(w).is_empty());
    let mem = SruInst::Run {
        a: Some(0x1000),
        astride: 16,
        out: 0x2000,
        ostride: 8,
        b: 0x3000,
        bstride: 16,
        src: Coord(0, 0),
        to: None,
        buf: 0,
        at: 0,
        count: false,
        words: 99,
    };
    let w = mem.encode(&s).unwrap()[0];
    assert_eq!(w.get(21, 24), 0);
    let got: Vec<u64> = s.addresses(w).iter().map(|a| a.value).collect();
    assert_eq!(got, [0x1000, 0x2000, 0x3000]);
}

#[test]
fn isa_sr_013_store() {
    let s = isa().sru;
    at(&s.store, "first", 248, 240);
    at(&s.store, "addr", 239, 200);
    at(&s.store, "rows", 199, 188);
    refused(
        SruInst::Store {
            addr: 0,
            rows: 4096,
            first: 0,
        }
        .encode(&s),
        "ISA.SR.013",
    );
}

#[test]
fn isa_sr_014_post() {
    let s = isa().sru;
    at(&s.post, "x", 251, 248);
    at(&s.post, "y", 247, 244);
    at(&s.post, "n", 15, 0);
    let w = SruInst::Post {
        dst: Coord(2, 2),
        n: 64,
    }
    .encode(&s)
    .unwrap()[0];
    assert_eq!((w.get(252, 4), w.get(0, 16)), (5, 64));
}

#[test]
fn isa_sr_020_global_fields() {
    let want = [
        ("NW", (0, 0, 12)),
        ("NPASS", (0, 12, 2)),
        ("BUF", (0, 14, 1)),
        ("USE_B", (0, 15, 1)),
        ("OUT", (0, 16, 2)),
        ("MASK", (0, 18, 1)),
        ("R1", (0, 19, 1)),
        ("NSR", (1, 0, 16)),
        ("ROW0", (1, 16, 9)),
        ("COL0", (2, 0, 9)),
        ("DHI", (2, 16, 16)),
        ("CLIM", (3, 0, 16)),
        ("MVAL", (4, 0, 24)),
        ("COLOFF", (5, 0, 16)),
        ("ROWOFF", (5, 16, 16)),
        ("K0", (8, 0, 24)),
        ("K3", (0xB, 0, 24)),
    ];
    for (n, slot) in want {
        assert_eq!(lookup(G, n), Some(slot), "{n}");
    }
    let job = Job {
        g: [("DHI".to_string(), -1i64), ("NW".to_string(), 16)].into(),
        ..Job::default()
    };
    let w = words(&job, &Pool::of(&sru_machine()).unwrap()).unwrap();
    assert_eq!(w, BTreeMap::from([(0, 16), (2, 0xFFFF << 16)]));
}

#[test]
fn isa_sr_021_pass_fields() {
    for (n, slot) in [
        ("NOPS", (0, 0, 4)),
        ("OPBASE", (0, 4, 4)),
        ("R2EN", (0, 8, 1)),
        ("EMIT", (0, 9, 1)),
        ("FINLEN", (0, 10, 4)),
        ("FINBASE", (0, 14, 4)),
    ] {
        assert_eq!(lookup(P, n), Some(slot), "{n}");
    }
    let job = Job {
        passes: vec![BTreeMap::new(), [("EMIT".to_string(), 1i64)].into()],
        ..Job::default()
    };
    let w = words(&job, &Pool::of(&sru_machine()).unwrap()).unwrap();
    assert_eq!(w.get(&0x12), Some(&(1 << 9)));
}

#[test]
fn isa_sr_022_op_store() {
    for (n, slot) in [
        ("KIND", (0, 0, 1)),
        ("PRE", (0, 20, 6)),
        ("NEG", (0, 30, 1)),
        ("KB", (1, 0, 24)),
        ("KW", (1, 24, 5)),
        ("KC", (2, 0, 24)),
        ("D0", (3, 0, 5)),
        ("D1", (3, 5, 5)),
    ] {
        assert_eq!(lookup(O, n), Some(slot), "{n}");
    }
    let pool = Pool::of(&sru_machine()).unwrap();
    let prog = [op(&[("A", name("O")), ("B", name("X")), ("C", name("H"))])];
    let s = schedule(&prog, &pool).unwrap();
    assert_eq!(
        (s.fields[0]["A"], s.fields[0]["B"], s.fields[0]["C"]),
        (2, 4, 5)
    );
    refused(schedule(&[op(&[("A", name("K"))])], &pool), "ISA.SR.022");
}

#[test]
fn isa_sr_023_finalise_word() {
    for (n, slot) in [
        ("OP", (0, 5)),
        ("DST", (5, 2)),
        ("A", (7, 4)),
        ("B", (11, 4)),
        ("C", (15, 4)),
    ] {
        assert_eq!(lookup(F, n), Some(slot), "{n}");
    }
    let fin =
        [("OP", 6i64), ("DST", 2), ("A", 0), ("B", 1), ("C", 9)].map(|(k, v)| (k.to_string(), v));
    let job = Job {
        fin: vec![fin.into()],
        ..Job::default()
    };
    let w = words(&job, &Pool::of(&sru_machine()).unwrap()).unwrap();
    assert_eq!(w[&0x80], 6 | 2 << 5 | 1 << 11 | 9 << 15);
}

#[test]
fn isa_sr_024_field_that_does_not_fit() {
    let pool = Pool::of(&sru_machine()).unwrap();
    let job = Job {
        passes: vec![[("EMIT".to_string(), 2i64)].into()],
        ..Job::default()
    };
    refused(words(&job, &pool), "ISA.SR.024");
    let job = Job {
        g: [("NOPE".to_string(), 1i64)].into(),
        ..Job::default()
    };
    refused(words(&job, &pool), "ISA.SR.024");
}

#[test]
fn isa_sr_030_modulo_schedule() {
    let mut pool = Pool::of(&sru_machine()).unwrap();
    let prog = vec![op(&[]), op(&[]), op(&[])];
    for (lanes, m) in [(16, 1), (8, 2), (4, 4)] {
        pool.f_lanes = lanes;
        let s = schedule(&prog, &pool).unwrap();
        assert_eq!(s.ii, 3 * m);
        assert_eq!(s.sigma, vec![0, m, 2 * m]);
    }
}

#[test]
fn isa_sr_031_result_latency_and_tap_depth() {
    let pool = Pool::of(&sru_machine()).unwrap();
    let prog = vec![op(&[]), op(&[("A", tap(0))])];
    let s = schedule(&prog, &pool).unwrap();
    assert!(s.sigma[1] - s.sigma[0] >= pool.ldep);
    assert_eq!(s.fields[1]["D0"], s.sigma[1] - s.sigma[0] - pool.ldep);
    assert_eq!(s.fields[1]["A"], 3);
    assert_eq!(s.fields[1]["HA"], 0);
    assert_eq!(s.fields[0]["KW"], (pool.wlead + s.ii - 1) / s.ii);
}

#[test]
fn isa_sr_032_schedule_refusals() {
    let mut pool = Pool::of(&sru_machine()).unwrap();
    refused(schedule(&[op(&[("A", tap(0))])], &pool), "ISA.SR.032");
    let three = vec![op(&[]), op(&[]), op(&[("A", tap(0)), ("B", tap(1))])];
    pool.h_taps = 1;
    refused(schedule(&three, &pool), "ISA.SR.032");
    pool.h_taps = 2;
    pool.f_lanes = 4;
    refused(
        schedule(
            &[
                op(&[]),
                op(&[]),
                op(&[]),
                op(&[("A", tap(0)), ("C", tap(1))]),
            ],
            &Pool { taps: 1, ..pool },
        ),
        "ISA.SR.032",
    );
}

#[test]
fn isa_sr_061_e8_constants() {
    assert_eq!(e8(1.0), 0x3F8000);
    assert_eq!(e8(-1.0), 0xBF8000);
    assert_eq!(e8(f64::INFINITY), 0x7F8000);
    assert_eq!(e8(f64::NAN), 0x7FC000);
    assert_eq!(e8(1e-40), 0);
    assert_eq!(e8(1.0 + 2f64.powi(-16)), 0x3F8000);
    assert_eq!(e8(1.0 + 3.0 * 2f64.powi(-16)), 0x3F8002);
}
