//! One test per scheduler clause (docs/spec/stack/l1/program.md
//! L1.SCHED.001-.006) and RUL.063, on small images.

use ktpu_l1::sched::{movable, preds, runs, schedule_module, Ruled};
use ktpu_l1::text::{emit, read, Module, Stmt};
use ktpu_machine::Machine;
use serde_json::json;

fn v9t6() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn module(body: &[&str]) -> Module {
    let mut text = String::from("target ktpu.v9\nimage k()\n");
    for l in body {
        text.push_str("    ");
        text.push_str(l);
        text.push('\n');
    }
    read(&text, "<k>").unwrap()
}

fn stmts(m: &Module) -> Vec<Stmt> {
    m.images["k"].stmts.clone()
}

fn order(m: &Module, machine: &Machine) -> Vec<usize> {
    let r = m.reader();
    let ruled = Ruled::new(&r, machine, m.vregs, m.vcopies).unwrap();
    ruled.order(&stmts(m)).unwrap()
}

fn cycles(m: &Module, machine: &Machine, order: &[usize]) -> i64 {
    let r = m.reader();
    let ruled = Ruled::new(&r, machine, m.vregs, m.vcopies).unwrap();
    ruled.cycles(&stmts(m), order).unwrap()
}

/// Two independent chains: an unpack feeding two dependent exp2s, and an
/// unpack feeding a multiply; the second chain can fill the first's waits.
const TWO_CHAINS: [&str; 5] = [
    "vunpk v0 <- l1[0] f16",
    "vexp2 v1, v0",
    "vexp2 v2, v1",
    "vunpk v8 <- l1[8] f16",
    "vmul v9, v8, v8",
];

#[test]
fn l1_sched_001_runs_are_bounded_by_fixed_statements() {
    let m = module(&[
        "vunpk v0 <- l1[0] f16",
        "vexp2 v1, v0",
        "vsync pack on=math",
        "vpack v1 -> l1[16] f16",
        "halt",
    ]);
    let body = stmts(&m);
    let movable: Vec<bool> = body.iter().map(movable).collect();
    assert_eq!(movable, [true, true, false, true, false]);
    let got: Vec<usize> = runs(&body).iter().map(Vec::len).collect();
    assert_eq!(got, [2, 1]);
}

#[test]
fn l1_sched_002_dependences() {
    let m = module(&[
        "vexp2 v1, v0",
        "vexp2 v2, v1",
        "vpack v2 -> l1[0] f16",
        "vunpk v5 <- l1[4] f16",
        "vunpk v6 <- l1[16] f16",
        "vmul v7, v5, v6",
    ]);
    let p = preds(&stmts(&m), v9t6().vc.chunks);
    assert!(p[1].contains(&0), "v1 read after its write");
    assert!(
        p[3].contains(&2),
        "l1[4] read after the pack writes l1[0..8]"
    );
    assert!(!p[4].contains(&2), "l1[16..24] is not the pack's");
    assert!(p[5].contains(&3) && p[5].contains(&4));
    // A core of four-word registers packs l1[0..4]: l1[4] is not the pack's.
    assert!(!preds(&stmts(&m), 4)[3].contains(&2));
}

#[test]
fn l1_sched_rul_063_the_core_shape_is_the_machines() {
    // One unpack and a full-length multiply on it, then one at vl=32.
    let m = module(&[
        "vunpk v0 <- l1[0] f16",
        "vmul v1, v0, v0",
        "vmul v2, v0, v0 vl=32",
    ]);
    let base = v9t6();
    let all: Vec<usize> = (0..3).collect();
    let at = |facts: &[(&str, u32)]| {
        let mut mach = base.clone();
        for (k, v) in facts {
            mach = mach.with_fact(k, json!(v)).unwrap();
        }
        cycles(&m, &mach, &all)
    };
    let here = at(&[]);
    assert_ne!(at(&[("vc.lanes", 8)]), here, "vc.lanes sets a vl's beats");
    assert_ne!(
        at(&[("vc.chunks", 4), ("vc.vlmax", 64)]),
        here,
        "vc.chunks sets a full op's beats"
    );
    // The constant shape the timing held before reads no fact: the variants
    // time the same.
    #[cfg(feature = "mutants")]
    {
        let fixed = |f: &[(&str, u32)]| ktpu_l1::mutant::with("l1.sched/fixed-shape", || at(f));
        assert_eq!(fixed(&[("vc.lanes", 8)]), fixed(&[]));
    }
}

#[test]
fn l1_sched_003_rul_063_model_reads_the_machine_timing() {
    let m = module(&TWO_CHAINS);
    let base = v9t6();
    let written: Vec<usize> = (0..5).collect();
    let slow = base.with_fact("vc.timing.unpack_math", json!(40)).unwrap();
    let a = cycles(&m, &base, &written);
    let b = cycles(&m, &slow, &written);
    // The critical chain starts with the measured unpack -> math edge.
    assert_eq!(
        b - a,
        40 - i64::from(base.vc.timing.unpack_math),
        "{a} -> {b}"
    );
}

#[test]
fn rul_063_configuration_costs_only_what_the_encoder_writes() {
    let (s, m) = config_cost_cycles();
    assert!(m - s > 4, "{s} -> {m}");
}

/// Eight independent one-beat ops: the front end, a word a cycle, is the
/// bound. One VL throughout needs one configuration word in all; VLs
/// alternating 16 / 32 make the encoder write one before every op, and add
/// only 4 beats of math, so anything past 4 more cycles is dispatch.
/// `(cycles with one VL, cycles with alternating VLs)`.
pub fn config_cost_cycles() -> (i64, i64) {
    let base = v9t6();
    let ops = |vl: &dyn Fn(usize) -> u32| -> Module {
        let lines: Vec<String> = (0..8)
            .map(|k| {
                format!(
                    "vadd v{}, v{}, v{} vl={}",
                    3 * k + 1,
                    3 * k + 2,
                    3 * k + 3,
                    vl(k)
                )
            })
            .collect();
        let refs: Vec<&str> = lines.iter().map(String::as_str).collect();
        module(&refs)
    };
    let same = ops(&|_| 16);
    let mixed = ops(&|k| if k % 2 == 0 { 16 } else { 32 });
    let all: Vec<usize> = (0..8).collect();
    (cycles(&same, &base, &all), cycles(&mixed, &base, &all))
}

#[test]
fn l1_sched_004_short_runs_kept_and_min_gain() {
    let m = module(&["vunpk v0 <- l1[0] f16", "vexp2 v1, v0"]);
    assert_eq!(order(&m, &v9t6()), [0, 1]);
    let m = module(&TWO_CHAINS);
    let base = v9t6();
    let got = order(&m, &base);
    let written: Vec<usize> = (0..5).collect();
    if got != written {
        let (g, w) = (cycles(&m, &base, &got), cycles(&m, &base, &written));
        assert!((g as f64) <= w as f64 * 0.99, "{g} vs {w}");
    }
    for (pos, &k) in got.iter().enumerate() {
        for &p in &preds(&stmts(&m), base.vc.chunks)[k] {
            assert!(got[..pos].contains(&p), "{k} before its predecessor {p}");
        }
    }
}

#[test]
fn l1_sched_005_image_kept_unless_timing_is_faster() {
    let m = module(&["vunpk v0 <- l1[0] f16", "vexp2 v1, v0", "halt"]);
    let got = schedule_module(&m, &v9t6()).unwrap();
    assert_eq!(emit(&got.images["k"].stmts), emit(&stmts(&m)));
}

#[test]
fn measured_front_end_costs_are_independent_schema_inputs() {
    use ktpu_l1::sched::timing;
    let base = v9t6();
    for (instruction, field, expected) in [
        ("seti s0 = 16", "vc.timing.seti", 3),
        ("setvl s0", "vc.timing.cond", 2),
    ] {
        let text = format!("image k()\n    {instruction}\n    {instruction}\n    halt\n");
        let at = |m: &Machine| timing::cycles(&text, "k", 128, 2, (0, 1), m).unwrap();
        let changed = base.with_fact(field, json!(expected + 1)).unwrap();
        // HALT also uses cond, whereas SETI is independent of it.
        assert_eq!(
            at(&changed) - at(&base),
            if field.ends_with("cond") { 3 } else { 2 }
        );
    }
    let mut p = timing::Params::of(&base);
    for mode in ["mx7a", "mx7b"] {
        let text = format!("vpack {{0}} -> l1[0] {mode}");
        assert_eq!(timing::parse(&text, &["r".into()], 2, &p).occ, 17);
        p.mx7_issue_gap += 1;
        assert_eq!(timing::parse(&text, &["r".into()], 2, &p).occ, 18);
        p.mx7_issue_gap -= 1;
        assert_eq!(p.mx7_pack_words, 16);
    }
}

#[test]
fn l1_sched_006_deterministic() {
    let m = module(&TWO_CHAINS);
    let a = schedule_module(&m, &v9t6()).unwrap();
    let b = schedule_module(&m, &v9t6()).unwrap();
    assert_eq!(a.source, b.source);
}
