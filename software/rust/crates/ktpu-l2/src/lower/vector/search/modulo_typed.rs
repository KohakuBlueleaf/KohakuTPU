//! Test-only modulo derivation and schedule differentials at the search boundary.

use super::*;
use crate::lower::vector::gen::{
    print,
    typed::{self as ir, Event},
};
use ktpu_l1::text::{Module, Stmt, Term};
use std::collections::{BTreeMap, HashMap};

mod tests;

static COVERAGE: Mutex<BTreeMap<&'static str, usize>> = Mutex::new(BTreeMap::new());

fn count(key: &'static str, n: usize) {
    *COVERAGE.lock().unwrap().entry(key).or_default() += n;
}

pub(in crate::lower::vector) fn reset() {
    COVERAGE.lock().unwrap().clear();
}

/// Checks the counts over `cases`; `whole` (every golden case) also requires
/// each path to have been taken.
pub(in crate::lower::vector) fn report(cases: usize, whole: bool) {
    let c = COVERAGE.lock().unwrap();
    eprintln!("typed modulo witness: {cases} cases; {c:?}; mismatches=0");
    let n = |key: &str| c.get(key).copied().unwrap_or(0);
    if whole {
        for key in PATHS {
            assert!(n(key) > 0, "missing modulo coverage {key}");
        }
    }
    assert_eq!(n("plans"), n("scheduled") + n("refused"));
    assert_eq!(n("tail-explicit"), n("scheduled"));
    assert_eq!(n("tail-kernel"), n("scheduled"));
    assert_eq!(n("images"), n("L1-fit"));
}

/// The modulo paths the golden cases take between them.
const PATHS: [&str; 18] = [
    "trips",
    "ops",
    "cross-reads",
    "predicate",
    "U",
    "M",
    "P",
    "plans",
    "scheduled",
    "refused",
    "rebuilt-accepted",
    "tail-explicit",
    "tail-kernel",
    "periodicity-comparisons",
    "L1-fit",
    "IMEM-fit",
    "IMEM-reject",
    "images",
];

pub(super) fn derive(
    placed: &[Event],
    head: usize,
    trip: &[Code],
    expected: &[MO::MOp],
    copies: u32,
    core: &MO::Core,
) -> (Vec<MO::MOp>, Vec<Stmt>) {
    let typed_ops: Vec<_> = placed[head..]
        .iter()
        .filter_map(|e| match e {
            Event::Op { stmt, syms } => Some((stmt, syms)),
            _ => None,
        })
        .collect();
    let legacy_ops: Vec<_> = trip
        .iter()
        .filter_map(|e| match e {
            Code::Op(tmpl, _) => Some(tmpl),
            _ => None,
        })
        .collect();
    assert_eq!(typed_ops.len(), legacy_ops.len());
    let got: Vec<_> = typed_ops
        .iter()
        .zip(legacy_ops)
        .map(|((st, syms), tmpl)| {
            let mut op = MO::derive(st, syms, copies, core);
            op.tmpl = tmpl.clone();
            op
        })
        .collect();
    assert_eq!(got, expected, "all MOp fields before linking");
    let mut a = expected.to_vec();
    let mut z = got.clone();
    MO::internals::link(&mut a, &core.timing);
    MO::internals::link(&mut z, &core.timing);
    assert_eq!(z, a, "ordered RAW/WAR/WAW dependences");
    count("trips", 1);
    count("ops", got.len());
    for o in &got {
        count(
            match o.eng {
                'U' => "U",
                'M' => "M",
                _ => "P",
            },
            1,
        );
        count("cross-reads", o.xreads.len());
        count(
            "predicate",
            usize::from(o.reads.iter().chain(&o.writes).any(|s| s == MO::PRED)),
        );
    }
    (got, typed_ops.iter().map(|(st, _)| (*st).clone()).collect())
}

#[allow(clippy::too_many_arguments)]
pub(super) fn plan(
    expected: &Option<MO::Plan>,
    ops: &[MO::MOp],
    regs: i64,
    pinned: &HashMap<String, i64>,
    k_max: i64,
    ii_max: i64,
    lead: i64,
    ii_min: i64,
    core: &MO::Core,
) -> Option<MO::Plan> {
    let last = MO::internals::last_ii();
    let got = MO::plan(ops, regs, pinned, k_max, ii_max, lead, ii_min, core);
    assert_eq!(MO::internals::last_ii(), last, "last placement attempt");
    count("plans", 1);
    match (expected, &got) {
        (None, None) => count("refused", 1),
        (Some(a), Some(z)) => {
            assert_eq!(z.ops, a.ops, "scheduled ops, dependencies, t and e");
            assert_eq!(
                (z.ii, z.k, z.stages, z.fill_t, z.drain_t, z.s0),
                (a.ii, a.k, a.stages, a.fill_t, a.drain_t, a.s0)
            );
            assert_eq!(z.phys, a.phys, "every symbol/copy physical register");
            assert_eq!(z.core, a.core);
            count("scheduled", 1);
        }
        (a, z) => panic!("plan outcome mismatch: {a:?} / {z:?}"),
    }
    got
}

// Match the generated raw-statement domain, including prefix-accepted sync options.
fn rebuilt(event: &Event) -> bool {
    match event {
        Event::Op { .. } => true,
        Event::Fixed(st) => match st.op.as_str() {
            "vbar" | "vdrain" => true,
            "vsync" => matches!((st.args.first(), st.kw("on")),
                (Some(Term::Name(w)), Some(Term::Name(on)))
                if (w == "pack" && on == "drain") || (w == "drain" && on == "pack")),
            _ => false,
        },
        _ => false,
    }
}

pub(super) fn eligibility(events: &[Event], legacy: &[Code]) {
    assert_eq!(events.len(), legacy.len());
    for (event, code) in events.iter().zip(legacy) {
        let expected = match code {
            Code::Op(_, _) => true,
            Code::Raw(s) => REBUILT.iter().any(|r| s.starts_with(r)),
        };
        assert_eq!(rebuilt(event), expected, "REBUILT: {code:?}");
        count(
            if expected {
                "rebuilt-accepted"
            } else {
                "rebuilt-refused"
            },
            1,
        );
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn emission(
    expected: &std::result::Result<Option<(Vec<String>, i64)>, String>,
    p: &MO::Plan,
    stmts: &[Stmt],
    rings: &[MO::Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
    top: i64,
    land: i64,
) -> Option<Vec<Event>> {
    let got = MO::emit_typed::emit(p, stmts, rings, trips, pinned, tail);
    let (comparisons, rejected) = MO::emit_typed::check_periodicity();
    count("periodicity-comparisons", comparisons);
    count("periodicity-reject", usize::from(rejected));
    count(
        if tail == "explicit" {
            "tail-explicit"
        } else {
            "tail-kernel"
        },
        1,
    );
    match (expected, got) {
        (Ok(Some((lines, cycles))), Ok(Some((events, actual_cycles)))) => {
            assert_eq!(*cycles, actual_cycles);
            let actual = print::emit(&mut ir::nest(&events).unwrap()).unwrap();
            assert_eq!(
                actual,
                indent(lines).join("\n") + "\n",
                "II {} K {} {tail}",
                p.ii,
                p.k
            );
            count(if top <= land { "L1-fit" } else { "L1-reject" }, 1);
            Some(events)
        }
        (Ok(None), Ok(None)) => {
            count("no-loop-body", 1);
            None
        }
        (Err(a), Err(z)) => {
            assert_eq!(*a, z);
            count("emit-error", 1);
            None
        }
        (a, z) => panic!("tail verdict mismatch: {a:?} / {z:?}"),
    }
}

pub(super) fn image(
    descs: &[crate::lower::vector::Desc],
    vregs: u32,
    name: &str,
    head: &[Event],
    tail: &[Event],
    pinned: &HashMap<String, i64>,
) -> Module {
    let mut body = vec![];
    for d in descs {
        body.push(
            ir::desc(
                &format!("a{}", d.ad),
                Term::name(format!("p_a{}", d.ad)),
                &d.walk.0,
            )
            .unwrap(),
        );
    }
    for d in descs {
        if d.inc != 0 {
            body.push(ir::ainc(&format!("a{}", d.ad), d.inc));
        }
    }
    let mut events = vec![];
    for e in head {
        match e {
            Event::Op { stmt, .. } => {
                let mut st = stmt.clone();
                MO::emit_typed::resolve(&mut st, &|s| pinned[s]);
                events.push(Event::Fixed(st));
            }
            _ => events.push(e.clone()),
        }
    }
    events.extend_from_slice(tail);
    events.push(Event::Fixed(ir::instruction("halt", vec![], vec![])));
    body.extend(ir::nest(&events).unwrap());
    let params = descs
        .iter()
        .filter(|d| d.to.is_empty())
        .map(|d| format!("p_a{}", d.ad))
        .collect();
    print::image(name, params, body, vregs).unwrap()
}

pub(super) fn sizing(
    module: &Module,
    expected: &crate::ir::Result<usize>,
    name: &str,
    nparams: usize,
    t: &Target,
) {
    let got = encode::words_of(&*throttled(module, t).unwrap(), name, nparams, &t.machine);
    assert_eq!(
        format!("{got:?}"),
        format!("{expected:?}"),
        "every sizing result"
    );
    count(
        match got {
            Err(_) => "encoding-reject",
            Ok(n) if n as i64 > IMEM_ROOM => "IMEM-reject",
            Ok(_) => "IMEM-fit",
        },
        1,
    );
    count("images", 1);
}
