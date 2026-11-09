//! Bank analysis and rotation over flat typed events, in encounter order.

use super::{Core, HashMap, HashSet, PASSES};
use crate::lower::vector::gen::typed::{self as ir, Event};
use ktpu_l1::text::{Stmt, Term};

fn walk(st: &Stmt) -> bool {
    matches!(st.op.as_str(), "vunpk" | "vpack")
}

fn fixed(st: &Stmt) -> bool {
    walk(st)
        && st
            .args
            .iter()
            .any(|t| matches!(t, Term::Name(n) if n.starts_with("mx7") || n == "gt4"))
}

/// First symbolic operand within a source, including crossbar calls.
pub(super) fn first(t: &Term) -> Option<(&str, u64, u64)> {
    match t {
        Term::Name(n) if n.starts_with('%') => Some((n, 0, 1)),
        Term::View(n, args) if n.starts_with('%') => {
            let (base, stride) = match args.as_slice() {
                [Term::Int(b, _)] => (*b as u64, 0),
                [Term::Slice(Some(b), None)] => (b.as_int().unwrap() as u64, 1),
                _ => panic!("unsupported generated register select {t:?}"),
            };
            Some((n, base, stride))
        }
        Term::Call(_, args) => args.iter().find_map(first),
        _ => None,
    }
}

fn reads(st: &Stmt) -> Vec<(&str, u64, u64)> {
    if walk(st) {
        return vec![];
    }
    st.args
        .iter()
        .filter(|a| a.as_kwarg().is_none())
        .skip(1)
        .filter_map(first)
        .collect()
}

fn beats(st: &Stmt, core: &Core) -> u64 {
    core.beats(st.kw("vl").and_then(Term::as_int).map(|n| n as u64))
}

pub fn choose(code: &[Event], copies: u32, core: &Core) -> HashMap<String, u64> {
    choose_profiled(code, copies, core, false).0
}

/// Exact incidence-index comparison for stage witnesses. The reference scans
/// prior indices; production only compares the last monotonically inserted one.
#[doc(hidden)]
pub fn choose_profiled(
    code: &[Event], copies: u32, core: &Core, exhaustive: bool,
) -> (HashMap<String, u64>, u64) {
    let mut comparisons = 0;
    if copies >= 4 || crate::mutant::on("l2.banks/rotates-none") {
        return (HashMap::new(), comparisons);
    }
    let tuples: Vec<_> = code
        .iter()
        .filter_map(|e| match e {
            Event::Op { stmt, syms } => Some((stmt, syms)),
            _ => None,
        })
        .collect();
    let ops: Vec<_> = tuples
        .iter()
        .filter(|(st, _)| !walk(st))
        .map(|(st, _)| (reads(st), beats(st, core)))
        .collect();
    let mut fixed_syms: HashSet<String> = tuples
        .iter()
        .filter(|(st, _)| fixed(st))
        .flat_map(|(_, syms)| syms.iter().map(ir::Symbol::name))
        .collect();
    let mut vsel_counts: HashMap<String, usize> = HashMap::new();
    for (st, syms) in &tuples {
        if st.op == "vsel" {
            for sym in *syms {
                *vsel_counts.entry(sym.name()).or_insert(0) += 1;
            }
        }
    }
    fixed_syms.extend(
        vsel_counts
            .into_iter()
            .filter_map(|(s, n)| (n > super::ASSEMBLED_VSELS).then_some(s)),
    );
    if !crate::mutant::on("l2.banks/rotates-predicated") {
        fixed_syms.extend(
            tuples
                .iter()
                .filter(|(st, _)| st.kw("if").is_some() || st.kw("unless").is_some())
                .filter_map(|(st, _)| {
                    st.args
                        .first()
                        .and_then(first)
                        .map(|(s, _, _)| s.to_string())
                }),
        );
    }
    let mut touching: Vec<(&str, Vec<usize>)> = Vec::new();
    let mut at = HashMap::new();
    for (k, (rs, _)) in ops.iter().enumerate() {
        for &(s, _, _) in rs {
            let i = *at.entry(s).or_insert_with(|| {
                touching.push((s, Vec::new()));
                touching.len() - 1
            });
            let seen = if exhaustive {
                touching[i].1.iter().any(|prior| {
                    comparisons += 1;
                    *prior == k
                })
            } else {
                // k increases in the outer loop. Repeated operands can only
                // duplicate this list's final entry, never an older entry.
                comparisons += 1;
                touching[i].1.last() == Some(&k)
            };
            if !seen {
                touching[i].1.push(k);
            }
        }
    }
    let free: Vec<usize> = (0..touching.len())
        .filter(|&i| !fixed_syms.contains(touching[i].0))
        .collect();
    // One instruction's price depends only on its operands' rotations. Its
    // operands, beat count and machine facts are fixed throughout this search.
    // Repeated coordinate-descent visits reuse the exact price, including for
    // repeated operands and predicated/fixed values; no search choice changes.
    let mut prices: Vec<HashMap<Vec<u64>, i64>> = vec![HashMap::new(); ops.len()];
    let mut price = |k: usize, rho: &HashMap<&str, u64>| {
        let rotations: Vec<u64> = ops[k]
            .0
            .iter()
            .map(|(s, _, _)| rho.get(s).copied().unwrap_or(0))
            .collect();
        *prices[k]
            .entry(rotations)
            .or_insert_with(|| super::cost(&ops[k].0, ops[k].1, rho, copies, core))
    };
    let mut rho = HashMap::new();
    for _ in 0..PASSES {
        let mut moved = false;
        for &i in &free {
            let (s, ks) = (&touching[i].0, &touching[i].1);
            let now: i64 = ks.iter().map(|&k| price(k, &rho)).sum();
            let flip = 1 - rho.get(s).copied().unwrap_or(0);
            rho.insert(*s, flip);
            let then: i64 = ks.iter().map(|&k| price(k, &rho)).sum();
            if then < now {
                moved = true;
            } else {
                rho.insert(*s, 1 - flip);
            }
        }
        if !moved {
            break;
        }
    }
    let rotations = rho.into_iter()
        .filter(|(_, r)| *r != 0)
        .map(|(s, r)| (s.to_string(), r))
        .collect();
    (rotations, comparisons)
}

fn rotated(t: &mut Term, rho: &HashMap<String, u64>, chunks: u64, cb: &mut u64) {
    if let Some((s, base, stride)) = first(t) {
        if matches!(t, Term::Name(_) | Term::View(..)) {
            let rot = rho.get(s).copied().unwrap_or(0);
            if rot != 0 {
                let select = if matches!(t, Term::Name(_)) {
                    ir::Select::Stride(rot)
                } else {
                    *cb = base;
                    if stride == 1 {
                        ir::Select::Stride((base + rot) % chunks)
                    } else {
                        ir::Select::Chunk((base + rot) % chunks)
                    }
                };
                *t = ir::reg(s, select);
            }
            return;
        }
    }
    if let Term::Call(_, args) = t {
        for a in args {
            rotated(a, rho, chunks, cb);
        }
    }
}

pub fn apply(st: &mut Stmt, syms: &[ir::Symbol], rho: &HashMap<String, u64>, chunks: u64) {
    if !syms
        .iter()
        .any(|s| rho.get(&s.name()).copied().unwrap_or(0) != 0)
    {
        return;
    }
    let mut cb = 0;
    for a in &mut st.args {
        rotated(a, rho, chunks, &mut cb);
    }
    if walk(st) && st.kw("n").is_none() {
        st.args.push(ir::kw("n", Term::int((chunks - cb).into())));
    }
}

pub fn rotate(mut code: Vec<Event>, copies: u32, core: &Core) -> Vec<Event> {
    if crate::mutant::banks_off() {
        return code;
    }
    let rho = choose(&code, copies, core);
    if !rho.is_empty() {
        for e in &mut code {
            if let Event::Op { stmt, syms } = e {
                apply(stmt, syms, &rho, core.chunks as u64);
            }
        }
    }
    if crate::mutant::on("l2.banks/drops-op") {
        if let Some(at) = code.iter().rposition(|e| matches!(e, Event::Op { .. })) {
            code.remove(at);
        }
    }
    code
}
