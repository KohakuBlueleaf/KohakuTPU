//! The decoder against regions laid out as debug.c writes them, and its
//! layout constants against the firmware headers they mirror.

use ktpu_debug::{decode, layout};
use ktpu_machine::Machine;
use std::path::PathBuf;

fn fw(rel: &str) -> String {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../firmware/kohakuaccel/include/ka")
        .join(rel);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{p:?}: {e}"))
}

/// `name`'s value in `text`: a `#define NAME value` or an enum item `NAME = value`.
fn value(text: &str, name: &str) -> u64 {
    for line in text.lines() {
        let t = line.trim();
        let item = t.match_indices(name).find_map(|(i, _)| {
            let before = t[..i].chars().last();
            let after = t[i + name.len()..].trim_start();
            (!before.is_some_and(|c| c.is_alphanumeric() || c == '_'))
                .then(|| after.strip_prefix('='))
                .flatten()
        });
        let rest = t.strip_prefix(&format!("#define {name} ")).or(item);
        if let Some(rest) = rest {
            let tok: String = rest
                .trim()
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric())
                .collect();
            let tok = tok.trim_end_matches(['u', 'U', 'L']);
            return match tok.strip_prefix("0x") {
                Some(h) => u64::from_str_radix(h, 16).unwrap(),
                None => tok.parse().unwrap(),
            };
        }
    }
    panic!("no {name}");
}

#[test]
fn layout_matches_debug_h_and_format_h() {
    let h = fw("package/debug.h");
    let pairs: [(&str, u64); 21] = [
        ("KA_DBG_MAGIC", layout::MAGIC),
        ("KA_DBG_VERSION", layout::VERSION),
        ("KA_DBG_H_MAGIC", layout::H_MAGIC as u64),
        ("KA_DBG_H_VERSION", layout::H_VERSION as u64),
        ("KA_DBG_H_LAST", layout::H_LAST as u64),
        ("KA_DBG_H_UNITS", layout::H_UNITS as u64),
        ("KA_DBG_H_REC", layout::H_REC as u64),
        ("KA_DBG_H_RECCAP", layout::H_RECCAP as u64),
        ("KA_DBG_H_WR", layout::H_WR as u64),
        ("KA_DBG_H_LOST", layout::H_LOST as u64),
        ("KA_DBG_H_RD", layout::H_RD as u64),
        ("KA_DBG_LASTS", layout::LASTS as u64),
        ("KA_DBG_REC_BYTES", layout::REC_BYTES as u64),
        ("KA_DBG_STEP_ENGINE", layout::STEP_ENGINE),
        ("KA_DBG_FAIL_ENGINE", layout::FAIL_ENGINE),
        ("KA_DBG_FAIL_DRAINED", layout::FAIL_DRAINED),
        ("KA_DEBUG_F_STEPS", layout::F_STEPS),
        ("KA_DEBUG_F_FAIL", layout::F_FAIL),
        ("KA_DEBUG_F_LAST", layout::F_LAST),
        ("KA_DBG_STEP", layout::STEP),
        ("KA_DBG_FAIL", layout::FAIL),
    ];
    for (name, want) in pairs {
        assert_eq!(value(&h, name), want, "{name}");
    }
    assert!(h.contains("KA_DBG_UNIT = 2") && layout::UNIT == 2);
    let i = fw("package/interp.h");
    assert_eq!(value(&i, "KA_RUN_F_TRACE"), layout::RUN_TRACE);
    assert_eq!(value(&i, "KA_RUN_F_NOENGINE"), layout::RUN_NOENGINE);
    let f = fw("package/format.h");
    for (code, op) in layout::OPS.iter().enumerate() {
        assert_eq!(value(&f, &format!("KA_OP_{op}")), code as u64, "{op}");
    }
}

/// A region as debug.c lays it out: N = 4, 32 unit blocks, ring of `cap`.
struct Region {
    b: Vec<u8>,
    rec: usize,
    cap: usize,
    wr: usize,
}

impl Region {
    fn new(cap: usize) -> Region {
        let rec = (layout::LASTS + 32 * 5 * 8 + 63) & !63;
        let mut r = Region {
            b: vec![0; rec + cap],
            rec,
            cap,
            wr: 0,
        };
        for (at, v) in [
            (layout::H_MAGIC, layout::MAGIC),
            (layout::H_VERSION, 1),
            (layout::H_LAST, 4),
            (layout::H_UNITS, 32),
            (layout::H_REC, rec as u64),
            (layout::H_RECCAP, cap as u64),
        ] {
            r.set(at, v);
        }
        r
    }

    fn set(&mut self, at: usize, v: u64) {
        self.b[at..at + 8].copy_from_slice(&v.to_le_bytes());
    }

    fn put(&mut self, w: [u64; 8]) {
        let base = self.rec + self.wr % self.cap;
        for (k, v) in w.iter().enumerate() {
            self.set(base + 8 * k, *v);
        }
        self.wr += 64;
        self.set(layout::H_WR, self.wr as u64);
    }

    fn completion(&mut self, unit: usize, count: u64, words: &[u64]) {
        let block = layout::LASTS + unit * 5 * 8;
        self.set(block, count);
        for (i, w) in words.iter().enumerate() {
            let c = count as usize - words.len() + i;
            self.set(block + 8 + (c % 4) * 8, *w);
        }
    }
}

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn cq(x: u64, y: u64, code: u64, arg: u64) -> u64 {
    y << 52 | x << 48 | code << 40 | arg << 8
}

#[test]
fn dbg_trace_001_step_records() {
    let mut r = Region::new(64 * 8);
    // DISPATCH (op 1) to unit 1, steps 2..4 as a group, firmware clock.
    r.put([
        1 | 1 << 16 | 1 << 32,
        0x83b,
        2 | 4 << 32,
        1234,
        5 | 3 << 32,
        5 | 2 << 32,
        0,
        0,
    ]);
    // AWAIT (op 2), engine clock.
    r.put([1 | 1 << 8 | 2 << 16, 0x83b, 5 | 5 << 32, 2000, 0, 0, 0, 0]);
    r.put([1 | 15 << 16 | 3 << 32, 0x83b, 6 | 6 << 32, 2100, 0, 0, 0, 0]);
    let got = decode(&r.b, &machine()).unwrap();
    assert_eq!(got.packages.len(), 1);
    let p = &got.packages[0];
    assert_eq!(p.tag, 0x83b);
    let s = &p.steps[0];
    assert_eq!(
        (s.first, s.last, s.op.as_str(), s.unit, s.cycle),
        (2, 4, "DISPATCH", Some(1), 1234)
    );
    assert_eq!(s.clock, "firmware");
    let c = s.counters.as_ref().unwrap();
    assert_eq!((c.sent, c.received, c.expected, c.inflight), (5, 3, 5, 2));
    let e = &p.steps[1];
    assert_eq!(
        (
            e.op.as_str(),
            e.clock.as_str(),
            e.unit,
            e.counters.is_none()
        ),
        ("AWAIT", "engine", Some(0), true)
    );
    // LOAD (op 15): [31:16] is a register, not a unit.
    let l = &p.steps[2];
    assert_eq!((l.op.as_str(), l.unit, l.field), ("LOAD", None, 3));
    assert_eq!(got.consumed, 192);
}

#[test]
fn dbg_fail_001_failure_report_named_from_the_schema() {
    let m = machine();
    let mut r = Region::new(64 * 8);
    let vc = 0x5643u64 << 32 | 2 << 8 | 1;
    r.put([2 | 1 << 32, 7, vc, 3 | 2 << 32, 3 | 1 << 32, 4 << 32, 3, 0]);
    let timeout = m.fw.status["KA_ST_TIMEOUT"] as u64;
    let await_ = m.fw.waits["KA_WAIT_AWAIT"] as u64;
    let mv = 1u64 << 33 | 6 << 28 | 17;
    r.put([
        3 | 2 << 8 | 2 << 16 | 1 << 32,
        7,
        timeout | await_ << 16 | 4 << 32,
        0xde01,
        mv,
        2,
        2,
        0,
    ]);
    let fault = m.fw.signals["KA_SIG_FAULT"] as u64;
    r.completion(
        1,
        6,
        &[
            cq(1, 2, 0, 1),
            cq(1, 2, 0, 2),
            cq(1, 2, 0, 3),
            cq(1, 2, fault, 5),
        ],
    );
    let got = decode(&r.b, &m).unwrap();
    let p = &got.packages[0];
    let u = &p.units[0];
    assert_eq!(
        (u.index, u.unit.kind.as_str(), u.unit.x, u.unit.y),
        (1, "VC", 1, 2)
    );
    let f = p.fail.as_ref().unwrap();
    assert_eq!(
        (
            f.status_name.as_str(),
            f.detail_name.as_str(),
            f.step,
            f.op.as_str()
        ),
        ("TIMEOUT", "AWAIT", 4, "AWAIT")
    );
    assert!(f.drained && !f.engine);
    assert_eq!(
        (f.mover.fault_name.as_str(), f.mover.room, f.mover.done),
        ("F_ALIGN", true, 17)
    );
    let last = &got.lasts[0];
    assert_eq!((last.index, last.count, last.words.len()), (1, 6, 4));
    let args: Vec<u32> = last.words.iter().map(|c| c.arg).collect();
    assert_eq!(args, [1, 2, 3, 5]);
    assert_eq!(last.words[3].fault.as_deref(), Some("F_LEN"));
}

#[test]
fn dbg_fail_005_engine_issue_count_beside_the_queued_step() {
    let m = machine();
    let timeout = m.fw.status["KA_ST_TIMEOUT"] as u64;
    let await_ = m.fw.waits["KA_WAIT_AWAIT"] as u64;
    let mut r = Region::new(64 * 8);
    // ENGINE (op 11) queued at step 3, the engine at issue 9; then a
    // firmware-path failure whose word 7 is not an issue count.
    r.put([
        3 | 1 << 8 | 11 << 16,
        5,
        timeout | await_ << 16 | 3 << 32,
        0xde01,
        0,
        0,
        1,
        9,
    ]);
    r.put([3 | 2 << 16, 6, timeout | await_ << 16, 0, 0, 0, 1, 7]);
    let got = decode(&r.b, &m).unwrap();
    let e = got.packages[0].fail.as_ref().unwrap();
    assert_eq!(
        (e.engine, e.step, e.op.as_str(), e.issued),
        (true, 3, "ENGINE", Some(9))
    );
    let f = got.packages[1].fail.as_ref().unwrap();
    assert_eq!((f.engine, f.issued), (false, None));
    assert!(e.engine_stat.is_none() && f.engine_stat.is_none());
}

#[test]
fn rul_032_engine_timeout_decodes_stat() {
    let m = machine();
    let timeout = m.fw.status["KA_ST_TIMEOUT"] as u64;
    let engine = m.fw.waits["KA_WAIT_ENGINE"] as u64;
    // STAT: magic 0xDE01, depth 2^6, head blocked, enabled, 9 issued, 3 queued.
    let stat = 0xde01u64 << 48 | 6 << 40 | 1 << 33 | 1 << 32 | 9 << 16 | 3;
    let mut r = Region::new(64 * 8);
    r.put([
        3 | 1 << 8 | 11 << 16,
        5,
        timeout | engine << 16 | 3 << 32,
        stat,
        0,
        0,
        1,
        9,
    ]);
    let got = decode(&r.b, &m).unwrap();
    let f = got.packages[0].fail.as_ref().unwrap();
    assert_eq!(f.detail_name, "ENGINE");
    let s = f.engine_stat.as_ref().unwrap();
    assert_eq!(
        (
            s.magic,
            s.depth,
            s.overflow,
            s.clearing,
            s.head_blocked,
            s.enabled,
            s.issued,
            s.queued
        ),
        (0xde01, 64, false, false, true, true, 9, 3)
    );
    let mut r = Region::new(64 * 8);
    r.put([
        3 | 1 << 8 | 11 << 16,
        5,
        timeout | engine << 16,
        stat & !(1 << 33),
        0,
        0,
        1,
        9,
    ]);
    let got = decode(&r.b, &m).unwrap();
    assert!(
        !got.packages[0]
            .fail
            .as_ref()
            .unwrap()
            .engine_stat
            .as_ref()
            .unwrap()
            .head_blocked
    );
}

#[test]
fn dbg_fail_003_unit_fault_decoded_by_kind() {
    let m = machine();
    let mut r = Region::new(64 * 8);
    let fault = m.fw.signals["KA_SIG_FAULT"] as u64;
    let word = cq(2, 1, fault, 12);
    r.put([2, 9, 0x5643u64 << 32 | 1 << 8 | 2, 0, 0, 0, 1, 0]);
    let unit_fault = m.fw.status["KA_ST_UNIT_FAULT"] as u64;
    r.put([3 | 1 << 16, 9, unit_fault, word, 0, 0, 1, 0]);
    let f = decode(&r.b, &m).unwrap().packages[0].fail.clone().unwrap();
    assert_eq!(f.detail_name, "unit 0");
    let c = f.completion.unwrap();
    assert_eq!(
        (c.signal.as_str(), c.fault.as_deref()),
        ("FAULT", Some("F_MODE"))
    );
}

#[test]
fn dbg_sess_002_records_grouped_by_tag_and_the_ring_wraps() {
    let mut r = Region::new(64 * 2);
    r.put([1, 1, 0, 0, 0, 0, 0, 0]);
    r.put([1, 1, 0, 0, 0, 0, 0, 0]);
    r.set(layout::H_RD, 128);
    r.put([1, 2, 0, 0, 0, 0, 0, 0]);
    let got = decode(&r.b, &machine()).unwrap();
    assert_eq!(got.packages.iter().map(|p| p.tag).collect::<Vec<_>>(), [2]);
    assert_eq!(got.consumed, 192);
}

#[test]
fn dbg_api_002_an_unreadable_record_fails() {
    let mut r = Region::new(64 * 2);
    r.put([9, 1, 0, 0, 0, 0, 0, 0]);
    assert_eq!(decode(&r.b, &machine()).unwrap_err().clause, "DBG.API.002");
    let mut bad = Region::new(64 * 2);
    bad.set(layout::H_MAGIC, 0);
    assert_eq!(
        decode(&bad.b, &machine()).unwrap_err().clause,
        "DBG.API.002"
    );
}

#[test]
fn dbg_trace_002_lost_records_are_reported() {
    let mut r = Region::new(64 * 2);
    r.set(layout::H_LOST, 64);
    assert_eq!(decode(&r.b, &machine()).unwrap().header.lost, 64);
}
