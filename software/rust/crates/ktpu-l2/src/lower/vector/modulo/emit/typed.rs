//! Scheduled statement events, ring operands and periodic loop construction.

use super::*;
use crate::lower::vector::gen::typed::{self as ir, Event};
use ktpu_l1::text::{Stmt, Term};
#[cfg(all(test, feature = "witness"))]
use std::cell::RefCell;

#[cfg(all(test, feature = "witness"))]
mod tests;

#[cfg(all(test, feature = "witness"))]
thread_local! {
    static PERIODICITY: RefCell<Option<[Vec<bool>; 2]>> = const { RefCell::new(None) };
}

#[cfg(all(test, feature = "witness"))]
pub(in crate::lower::vector) fn start_periodicity() {
    PERIODICITY.with_borrow_mut(|p| *p = Some(Default::default()));
}

#[cfg(all(test, feature = "witness"))]
pub(super) fn record_periodicity(side: usize, same: bool) {
    PERIODICITY.with_borrow_mut(|p| {
        if let Some(p) = p {
            p[side].push(same);
        }
    });
}

#[cfg(all(test, feature = "witness"))]
pub(in crate::lower::vector) fn check_periodicity() -> (usize, bool) {
    PERIODICITY.with_borrow_mut(|p| {
        let [a, z] = p.take().unwrap();
        assert_eq!(a, z, "every periodicity comparison");
        (a.len(), a.contains(&false))
    })
}

#[derive(Clone, Debug)]
enum Scheduled {
    Insn(Stmt, Option<&'static str>),
    Sync(&'static str, &'static str, Option<i64>),
}

pub(in crate::lower::vector) fn resolve(st: &mut Stmt, register: &dyn Fn(&str) -> i64) {
    fn operand(t: &mut Term, register: &dyn Fn(&str) -> i64) {
        match t {
            Term::Name(n) | Term::View(n, _) if n.starts_with('%') => {
                *n = format!("v{}", register(n));
            }
            Term::Call(_, args) => {
                for a in args {
                    operand(a, register);
                }
            }
            _ => {}
        }
    }
    for a in &mut st.args {
        operand(a, register);
    }
}

fn instance(
    st: &Stmt,
    o: &MOp,
    p: &Plan,
    rings: &[Ring],
    pinned: &HashMap<String, i64>,
    j: i64,
) -> Result<Stmt, String> {
    let mut st = st.clone();
    let copy = floor_mod(j, p.k);
    resolve(&mut st, &|s| {
        pinned
            .get(s)
            .copied()
            .unwrap_or_else(|| p.phys[&(s.into(), copy)])
    });
    if o.reads.iter().chain(&o.writes).any(|s| s == PRED) {
        let pred = format!("p{}", 1 + floor_mod(copy, pred_copies(p.k, p.core.preds)));
        if COMPARE.contains(&st.op.as_str()) && st.args.first() == Some(&Term::name("p1")) {
            st.args[0] = Term::name(&pred);
        }
        for arg in &mut st.args {
            if let Term::Assign(key, value) = arg {
                if matches!(key.as_ref(), Term::Name(k) if k == "if" || k == "unless")
                    && value.as_ref() == &Term::name("p1")
                {
                    **value = Term::name(&pred);
                }
            }
        }
    }
    if let Some(word) = o.l1 {
        let r = rings
            .iter()
            .find(|r| r.base <= word && word < r.base + r.words)
            .ok_or_else(|| format!("l1[{word}] is in no ring"))?;
        assert!(matches!(st.op.as_str(), "vunpk" | "vpack"));
        st.args[2] = ir::l1(r.slots[copy as usize] + word - r.base);
    }
    Ok(st)
}

// Generated statements have no positions yet; float bits preserve NaN periodicity.
fn same_stmt(a: &Stmt, b: &Stmt) -> bool {
    fn term(a: &Term, b: &Term) -> bool {
        match (a, b) {
            (Term::Float(x), Term::Float(y)) => x.to_bits() == y.to_bits(),
            (Term::Neg(x), Term::Neg(y)) => term(x, y),
            (Term::View(x, xs), Term::View(y, ys)) | (Term::Call(x, xs), Term::Call(y, ys)) => {
                x == y && terms(xs, ys)
            }
            (Term::Assign(x, v), Term::Assign(y, w)) => term(x, y) && term(v, w),
            _ => a == b,
        }
    }
    fn terms(a: &[Term], b: &[Term]) -> bool {
        a.len() == b.len() && a.iter().zip(b).all(|(a, b)| term(a, b))
    }
    a.op == b.op && a.commas == b.commas && terms(&a.args, &b.args)
}

pub(in crate::lower::vector) fn emit(
    p: &Plan,
    stmts: &[Stmt],
    rings: &[Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
) -> Result<Option<(Vec<Event>, i64)>, String> {
    let (ii, k, stages) = (p.ii, p.k, p.stages);
    assert_eq!(stmts.len(), p.ops.len());
    let mut order: Vec<_> = p
        .ops
        .iter()
        .enumerate()
        .map(|(i, o)| (o.t, i as i64))
        .collect();
    if let Some(t) = p.fill_t {
        order.push((t, FILL));
    }
    if let Some(t) = p.drain_t {
        order.push((t, DRAIN));
    }
    order.sort_by_key(|x| (floor_mod(x.0, ii), x.0));
    let ends = |eng| {
        let indices: Vec<_> = (0..p.ops.len()).filter(|&i| p.ops[i].eng == eng).collect();
        let first = indices.iter().copied().min_by_key(|&i| p.ops[i].t);
        // Reverse traversal keeps the first operation on ties, as the template does.
        let last = indices.iter().rev().copied().max_by_key(|&i| p.ops[i].t);
        (first, last)
    };
    let (first_u, last_u) = ends('U');
    let (first_p, last_p) = ends('P');
    let n_inst = trips + stages - 1;
    let lo = stages - 1;
    let reps = floor_div(0.max(if tail == "kernel" { n_inst } else { trips } - lo), k);
    let hi = lo + reps * k;
    dataflow(p, &order, trips, pinned, n_inst, tail, lo, hi)?;
    let mut seq = Vec::new();
    let mut counts = [0i64; 4];
    let mut done: [HashMap<i64, i64>; 4] = Default::default();
    let mut marks = Vec::new();
    macro_rules! wait {
        ($w:expr, $on:expr, $j:expr) => {{
            let j = $j;
            if j >= 0 && !done[kind_ix($on)].contains_key(&j) {
                return Ok(None);
            }
            seq.push(Scheduled::Sync(
                $w,
                $on,
                if j >= 0 {
                    Some(done[kind_ix($on)][&j])
                } else {
                    None
                },
            ));
        }};
    }
    macro_rules! insn {
        ($st:expr, $kind:expr) => {{
            let kind: Option<&'static str> = $kind;
            if let Some(kd) = kind {
                counts[kind_ix(kd)] += 1;
            }
            seq.push(Scheduled::Insn($st, kind));
        }};
    }
    for n in 0..n_inst {
        marks.push(seq.len());
        let past = tail == "kernel" && lo <= n && n < hi;
        for &(t, i) in &order {
            let j = n - p.stage(t);
            if j < 0 || (j >= trips && !past) {
                continue;
            }
            if i == FILL || i == DRAIN {
                let (kind, engine, memory) = if i == FILL {
                    wait!("fill", "unpack", j - k);
                    ("load", "fill", ir::Memory::Fill)
                } else {
                    if j >= trips {
                        return Ok(None);
                    }
                    wait!("drain", "pack", j);
                    ("store", "drain", ir::Memory::Drain)
                };
                for r in rings.iter().filter(|r| r.kind == kind) {
                    insn!(
                        ir::memory(
                            memory,
                            Term::name(format!("a{}", r.ad)),
                            r.slots[floor_mod(j, k) as usize],
                            None,
                            true,
                            vec![]
                        )?,
                        Some(engine)
                    );
                }
                done[kind_ix(engine)].insert(j, counts[kind_ix(engine)]);
            } else {
                let i = i as usize;
                match p.ops[i].eng {
                    'U' => {
                        if Some(i) == first_u && p.fill_t.is_some() {
                            wait!("unpack", "fill", j);
                        }
                        insn!(
                            instance(&stmts[i], &p.ops[i], p, rings, pinned, j)?,
                            Some("unpack")
                        );
                        if Some(i) == last_u {
                            done[1].insert(j, counts[1]);
                        }
                    }
                    'P' => {
                        if Some(i) == first_p && p.drain_t.is_some() {
                            wait!("pack", "drain", j - k);
                        }
                        insn!(
                            instance(&stmts[i], &p.ops[i], p, rings, pinned, j)?,
                            Some("pack")
                        );
                        if Some(i) == last_p {
                            done[2].insert(j, counts[2]);
                        }
                    }
                    _ => insn!(instance(&stmts[i], &p.ops[i], p, rings, pinned, j)?, None),
                }
            }
        }
    }
    marks.push(seq.len());
    let mut run = [0i64; 4];
    let exact: Vec<_> = seq
        .iter()
        .map(|e| match e {
            Scheduled::Insn(_, kind) => {
                if let Some(kd) = kind {
                    run[kind_ix(kd)] += 1;
                }
                None
            }
            Scheduled::Sync(_, on, need) => {
                Some(need.map_or(63, |nd| (run[kind_ix(on)] - nd).clamp(0, 63)))
            }
        })
        .collect();
    let put = |out: &mut Vec<Event>, a: usize, b: usize, slack: &dyn Fn(usize) -> Option<i64>| {
        for (q, ev) in seq.iter().enumerate().take(b).skip(a) {
            out.push(Event::Fixed(match ev {
                Scheduled::Insn(st, _) => st.clone(),
                Scheduled::Sync(w, on, _) => ir::sync(
                    w,
                    on,
                    Some(slack(q).map_or_else(|| Term::name("None"), |s| Term::dec(s.into()))),
                ),
            }));
        }
    };
    let mut out = vec![];
    put(&mut out, 0, marks[lo as usize], &|q| exact[q]);
    if reps > 0 {
        let (a, b) = (marks[lo as usize], marks[(lo + k) as usize]);
        for rep in 0..reps {
            let a2 = marks[(lo + rep * k) as usize];
            for q in 0..b - a {
                let same = match (&seq[a + q], &seq[a2 + q]) {
                    (Scheduled::Insn(x, _), Scheduled::Insn(y, _)) => same_stmt(x, y),
                    (Scheduled::Sync(w, on, _), Scheduled::Sync(w2, on2, _)) => {
                        w == w2 && on == on2
                    }
                    _ => false,
                };
                #[cfg(all(test, feature = "witness"))]
                record_periodicity(1, same);
                if !same {
                    return Ok(None);
                }
            }
        }
        let slack: Vec<_> = (0..b - a)
            .map(|q| {
                (0..reps)
                    .filter_map(|rep| exact[marks[(lo + rep * k) as usize] + q])
                    .min()
            })
            .collect();
        let reg = format!("s{}", p.core.loop_s);
        out.push(Event::Fixed(ir::seti(&reg, Term::dec(reps.into()))));
        out.push(Event::Fixed(ir::instruction(
            "loop",
            vec![Term::name(&reg)],
            vec![],
        )));
        out.push(Event::Open);
        put(&mut out, a, b, &|q| slack[q - a]);
        out.push(Event::Close);
    }
    put(&mut out, marks[hi as usize], marks[n_inst as usize], &|q| {
        exact[q]
    });
    Ok(Some((out, ii * n_inst)))
}
