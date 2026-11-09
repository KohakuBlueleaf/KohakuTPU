//! Scheduled statement events, ring operands and periodic loop construction.

use super::*;
use crate::lower::vector::gen::typed::{self as ir, Event};
use ktpu_l1::text::{Stmt, Term};

thread_local! {
    static MATERIALIZED: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
    static INTERVALS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// Instructions whose operands the last emission on this thread constructed.
#[doc(hidden)]
pub fn last_materialized() -> usize {
    MATERIALIZED.with(|count| count.get())
}

/// Initiation intervals replayed by the last emission on this thread.
#[doc(hidden)]
pub fn last_intervals() -> usize {
    INTERVALS.with(|count| count.get())
}

/// An instruction's periodic identity, before operand spelling.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Instruction {
    Op(usize, i64),
    Memory(usize, i64),
}

#[derive(Clone, Debug)]
enum Scheduled {
    Insn(Instruction, Option<&'static str>),
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
    address: Option<(usize, i64)>,
) -> Stmt {
    MATERIALIZED.with(|count| count.set(count.get() + 1));
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
        if COMPARE.contains(&st.op.as_str()) {
            match st.args.first_mut() {
                Some(Term::Name(n) | Term::View(n, _)) if n == "p1" => *n = pred.clone(),
                _ => {}
            }
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
    if let Some((ring, offset)) = address {
        st.args[2] = ir::l1(rings[ring].slots[copy as usize] + offset);
    }
    st
}

#[doc(hidden)]
pub fn emit(
    p: &Plan,
    stmts: &[Stmt],
    rings: &[Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
) -> Result<Option<(Vec<Event>, i64)>, String> {
    emit_inner(p, stmts, rings, trips, pinned, tail, true)
}

/// Exhaustive dynamic-trip replay for stage equivalence witnesses.
#[doc(hidden)]
pub fn emit_exhaustive(
    p: &Plan,
    stmts: &[Stmt],
    rings: &[Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
) -> Result<Option<(Vec<Event>, i64)>, String> {
    emit_inner(p, stmts, rings, trips, pinned, tail, false)
}

fn emit_inner(
    p: &Plan,
    stmts: &[Stmt],
    rings: &[Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
    periodic: bool,
) -> Result<Option<(Vec<Event>, i64)>, String> {
    MATERIALIZED.with(|count| count.set(0));
    INTERVALS.with(|count| count.set(0));
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
    let lo = stages - 1;
    // After the prologue all operations are active. One K-period writes
    // every rotating register; the next has steady register and queue state.
    // Further periods translate iteration tags and completion counts equally.
    // Keep both boundary periods and the complete tail, including speculative
    // kernel-tail operations, so legality and minimum sync slacks are unchanged.
    let omitted = if periodic {
        (floor_div(trips - lo, k) - 2).max(0) * k
    } else {
        0
    };
    let trips = trips - omitted;
    let n_inst = trips + lo;
    let reps = floor_div(0.max(if tail == "kernel" { n_inst } else { trips } - lo), k);
    let hi = lo + reps * k;
    dataflow(p, &order, trips, pinned, n_inst, tail, lo, hi)?;
    let mut seq = Vec::new();
    let mut counts = [0i64; 4];
    let mut done: [HashMap<i64, i64>; 4] = Default::default();
    let mut marks = Vec::new();
    let mut validated = vec![false; p.ops.len()];
    let mut addresses = vec![None; p.ops.len()];
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
            let instruction = $st;
            if let Instruction::Op(i, _) = instruction {
                if !validated[i] {
                    if let Some(word) = p.ops[i].l1 {
                        let (index, ring) = rings
                            .iter()
                            .enumerate()
                            .find(|(_, r)| r.base <= word && word < r.base + r.words)
                            .ok_or_else(|| format!("l1[{word}] is in no ring"))?;
                        assert!(matches!(stmts[i].op.as_str(), "vunpk" | "vpack"));
                        addresses[i] = Some((index, word - ring.base));
                    }
                    validated[i] = true;
                }
            }
            let kind: Option<&'static str> = $kind;
            if let Some(kd) = kind {
                counts[kind_ix(kd)] += 1;
            }
            seq.push(Scheduled::Insn(instruction, kind));
        }};
    }
    for n in 0..n_inst {
        INTERVALS.with(|count| count.set(count.get() + 1));
        marks.push(seq.len());
        let past = tail == "kernel" && lo <= n && n < hi;
        for &(t, i) in &order {
            let j = n - p.stage(t);
            if j < 0 || (j >= trips && !past) {
                continue;
            }
            if i == FILL || i == DRAIN {
                if j >= trips {
                    return Ok(None);
                }
                let (kind, engine) = if i == FILL {
                    wait!("fill", "unpack", j - k);
                    ("load", "fill")
                } else {
                    wait!("drain", "pack", j);
                    ("store", "drain")
                };
                for (index, _) in rings.iter().enumerate().filter(|(_, r)| r.kind == kind) {
                    insn!(Instruction::Memory(index, floor_mod(j, k)), Some(engine));
                }
                done[kind_ix(engine)].insert(j, counts[kind_ix(engine)]);
            } else {
                let i = i as usize;
                match p.ops[i].eng {
                    'U' => {
                        if Some(i) == first_u && p.fill_t.is_some() {
                            wait!("unpack", "fill", j);
                        }
                        insn!(Instruction::Op(i, floor_mod(j, k)), Some("unpack"));
                        if Some(i) == last_u {
                            done[1].insert(j, counts[1]);
                        }
                    }
                    'P' => {
                        if Some(i) == first_p && p.drain_t.is_some() {
                            wait!("pack", "drain", j - k);
                        }
                        insn!(Instruction::Op(i, floor_mod(j, k)), Some("pack"));
                        if Some(i) == last_p {
                            done[2].insert(j, counts[2]);
                        }
                    }
                    _ => insn!(Instruction::Op(i, floor_mod(j, k)), None),
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
    let put = |out: &mut Vec<Event>,
               a: usize,
               b: usize,
               slack: &dyn Fn(usize) -> Option<i64>|
     -> Result<(), String> {
        for (q, ev) in seq.iter().enumerate().take(b).skip(a) {
            out.push(Event::Fixed(match ev {
                Scheduled::Insn(Instruction::Op(i, copy), _) => instance(
                    &stmts[*i],
                    &p.ops[*i],
                    p,
                    rings,
                    pinned,
                    *copy,
                    addresses[*i],
                ),
                Scheduled::Insn(Instruction::Memory(index, copy), _) => {
                    let r = &rings[*index];
                    MATERIALIZED.with(|count| count.set(count.get() + 1));
                    ir::memory(
                        if r.kind == "load" {
                            ir::Memory::Fill
                        } else {
                            ir::Memory::Drain
                        },
                        Term::name(format!("a{}", r.ad)),
                        r.slots[*copy as usize],
                        None,
                        true,
                        vec![],
                    )?
                }
                Scheduled::Sync(w, on, _) => ir::sync(
                    w,
                    on,
                    Some(slack(q).map_or_else(|| Term::name("None"), |s| Term::dec(s.into()))),
                ),
            }));
        }
        Ok(())
    };
    let mut out = vec![];
    put(&mut out, 0, marks[lo as usize], &|q| exact[q])?;
    if reps > 0 {
        let (a, b) = (marks[lo as usize], marks[(lo + k) as usize]);
        for rep in 0..reps {
            let a2 = marks[(lo + rep * k) as usize];
            for q in 0..b - a {
                let same = match (&seq[a + q], &seq[a2 + q]) {
                    (Scheduled::Insn(x, _), Scheduled::Insn(y, _)) => x == y,
                    (Scheduled::Sync(w, on, _), Scheduled::Sync(w2, on2, _)) => {
                        w == w2 && on == on2
                    }
                    _ => false,
                };
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
        out.push(Event::Fixed(ir::seti(
            &reg,
            Term::dec((reps + omitted / k).into()),
        )));
        out.push(Event::Fixed(ir::instruction(
            "loop",
            vec![Term::name(&reg)],
            vec![],
        )));
        out.push(Event::Open);
        put(&mut out, a, b, &|q| slack[q - a])?;
        out.push(Event::Close);
    }
    put(&mut out, marks[hi as usize], marks[n_inst as usize], &|q| {
        exact[q]
    })?;
    Ok(Some((out, ii * (n_inst + omitted))))
}
