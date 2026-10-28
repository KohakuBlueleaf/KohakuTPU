//! Typed allocation with explicit metadata ordering and a test-only legacy trace.

use super::gen::typed::Event;
#[cfg(all(test, feature = "witness"))]
use super::Code;
use super::{HashMap, HashSet, VErr, VResult};
use ktpu_l1::text::Term;
#[cfg(all(test, feature = "witness"))]
use std::cell::RefCell;

type Trace = Vec<(String, u32)>;
#[cfg(all(test, feature = "witness"))]
thread_local! {
    static TRACE: RefCell<Option<Trace>> = const { RefCell::new(None) };
}

#[cfg(all(test, feature = "witness"))]
pub(super) fn record(s: &str, r: u32) {
    TRACE.with_borrow_mut(|trace| {
        if let Some(trace) = trace {
            trace.push((s.into(), r));
        }
    });
}

#[cfg(all(test, feature = "witness"))]
pub(super) fn reference(
    code: &[Code],
    pinned: &HashSet<String>,
    regs: u32,
) -> (VResult<Vec<String>>, Trace) {
    TRACE.with_borrow_mut(|trace| {
        assert!(trace.is_none());
        *trace = Some(vec![]);
    });
    let result = super::assign(code, pinned, regs);
    let trace = TRACE.with_borrow_mut(|trace| trace.take().unwrap());
    (result, trace)
}

fn resolve(t: &mut Term, reg: &HashMap<String, u32>) {
    match t {
        Term::Name(n) | Term::View(n, _) if n.starts_with('%') => *n = format!("v{}", reg[n]),
        Term::Call(_, args) => {
            for a in args {
                resolve(a, reg);
            }
        }
        _ => (),
    }
}

pub(super) fn assign(
    code: &[Event],
    pinned: &HashSet<String>,
    regs: u32,
) -> (VResult<Vec<Event>>, Trace) {
    let mut trace = vec![];
    let result = allocate(code, pinned, regs, &mut trace);
    (result, trace)
}

fn allocate(
    code: &[Event],
    pinned: &HashSet<String>,
    regs: u32,
    _trace: &mut Trace,
) -> VResult<Vec<Event>> {
    let mut last = HashMap::new();
    for (i, e) in code.iter().enumerate() {
        if let Event::Op { syms, .. } = e {
            for s in syms {
                last.insert(s.name(), i);
            }
        }
    }
    let mut reg: HashMap<String, u32> = HashMap::new();
    let mut free: Vec<u32> = (0..regs).collect();
    let mut out = vec![];
    for (i, e) in code.iter().enumerate() {
        let Event::Op { stmt, syms } = e else {
            out.push(e.clone());
            continue;
        };
        let syms: Vec<String> = syms.iter().map(|s| s.name()).collect();
        let mut dying: Vec<&String> = syms[1..]
            .iter()
            .filter(|s| last[*s] == i && reg.contains_key(*s) && !pinned.contains(*s))
            .collect();
        for (j, s) in syms.iter().enumerate() {
            if reg.contains_key(s) {
                continue;
            }
            if j == 0 && !dying.is_empty() {
                let d = dying.remove(0);
                let r = reg[d];
                reg.insert(s.clone(), r);
                #[cfg(all(test, feature = "witness"))]
                _trace.push((s.clone(), r));
                continue;
            }
            if free.is_empty() {
                return Err(VErr::Pressure);
            }
            let r = free.remove(0);
            reg.insert(s.clone(), r);
            #[cfg(all(test, feature = "witness"))]
            _trace.push((s.clone(), r));
        }
        let mut stmt = stmt.clone();
        for a in &mut stmt.args {
            resolve(a, &reg);
        }
        out.push(Event::Fixed(stmt));
        let held: HashSet<u32> = syms
            .iter()
            .filter(|s| last[*s] > i || pinned.contains(*s))
            .map(|s| reg[s])
            .collect();
        let mut seen = HashSet::new();
        for s in &syms {
            if !seen.insert(s) {
                continue;
            }
            let r = reg[s];
            if last[s] == i && !pinned.contains(s) && !held.contains(&r) && !free.contains(&r) {
                free.push(r);
            }
        }
        free.sort();
    }
    Ok(out)
}
