//! Scheduler facts derived directly from generated statements and symbol metadata.

use super::*;
use crate::lower::vector::gen::typed::Symbol;
use ktpu_l1::text::{Stmt, Term};

#[cfg(all(test, feature = "witness"))]
mod tests;

fn refs(t: &Term) -> Vec<(String, u64, u64)> {
    match t {
        Term::Name(n) if n.starts_with('%') => vec![(n.clone(), 0, 1)],
        Term::View(n, args) if n.starts_with('%') => {
            let (base, stride) = match args.as_slice() {
                [Term::Int(b, _)] => (*b as u64, 0),
                [Term::Slice(Some(b), None)] => (b.as_int().unwrap() as u64, 1),
                _ => panic!("unsupported generated select {t:?}"),
            };
            vec![(n.clone(), base, stride)]
        }
        Term::Call(_, args) => args.iter().flat_map(refs).collect(),
        _ => vec![],
    }
}

fn hold(st: &Stmt, operands: &[&Term], beats: u64, core: &Core) -> i64 {
    if !READS_B.contains(&st.op.as_str())
        || !READS_C.contains(&st.op.as_str())
        || operands.len() < 4
        || matches!(operands[3], Term::Call(..))
    {
        return 0;
    }
    let walks: Option<Vec<_>> = operands[1..4]
        .iter()
        .map(|t| refs(t).into_iter().next())
        .collect();
    let Some(walks) = walks else { return 0 };
    (0..beats)
        .filter(|i| {
            let words: HashSet<_> = walks
                .iter()
                .map(|(s, b, stride)| (s, (b + stride * i) % core.chunks as u64))
                .collect();
            let banks: HashSet<_> = words.iter().map(|(_, c)| c % core.rf_banks).collect();
            words.len() == 3 && banks.len() == 1
        })
        .count() as i64
}

pub fn derive(st: &Stmt, symbols: &[Symbol], copies: u32, core: &Core) -> MOp {
    let syms: Vec<_> = symbols.iter().map(Symbol::name).collect();
    let number = |key| st.kw(key).and_then(Term::as_int).map(|n| n as u64);
    let mode = |prefix: &str| {
        st.args
            .iter()
            .any(|t| matches!(t, Term::Name(n) if n.starts_with(prefix)))
    };
    let mut out = MOp {
        op: st.op.clone(),
        #[cfg(feature = "witness")]
        tmpl: String::new(),
        syms,
        eng: 'M',
        reads: vec![],
        writes: vec![],
        occ: 0,
        l1: None,
        t: 0,
        e: 0,
        deps: vec![],
        xreads: vec![],
    };
    if matches!(st.op.as_str(), "vunpk" | "vpack") {
        out.l1 = match &st.args[2] {
            Term::View(n, indices) if n == "l1" => Some(indices[0].as_int().unwrap() as i64),
            other => panic!("unsupported generated address {other:?}"),
        };
        if st.op == "vunpk" {
            let base = match &st.args[0] {
                Term::View(_, indices) => match indices.as_slice() {
                    [Term::Slice(Some(b), None)] => b.as_int().unwrap() as u64,
                    _ => 0,
                },
                _ => 0,
            };
            let chunks = number("n").unwrap_or(core.chunks as u64 - base);
            out.eng = 'U';
            if chunks < core.chunks as u64 {
                out.reads.push(out.syms[0].clone());
            }
            out.writes.push(out.syms[0].clone());
            out.occ = (chunks * if mode("f32") { 2 } else { 1 }) as i64;
        } else {
            out.eng = 'P';
            out.reads.push(out.syms[0].clone());
            let words = if mode("mx7") {
                (core.timing.mx7_pack_words + core.timing.mx7_issue_gap) as u64
            } else {
                number("n").unwrap_or(core.chunks as u64)
            };
            out.occ = (words * if mode("f32") { 2 } else { 1 }) as i64;
        }
        return out;
    }
    let operands = st.positional();
    let beats = core.beats(number("vl"));
    let mut reads = BTreeSet::new();
    let mut xreads = BTreeSet::new();
    for source in operands.iter().skip(1) {
        for (s, _, _) in refs(source) {
            reads.insert(s.clone());
            if matches!(source, Term::Call(..)) && !crate::mutant::on("l2.vector/xreads-dropped") {
                xreads.insert(s);
            }
        }
    }
    if COMPARE.contains(&st.op.as_str()) {
        out.writes.push(PRED.into());
    } else if let Some((s, _, _)) = operands.first().and_then(|t| refs(t).into_iter().next()) {
        out.writes.push(s);
    }
    if st.kw("if").is_some() || st.kw("unless").is_some() {
        reads.insert(PRED.into());
    }
    out.reads = reads.into_iter().collect();
    out.xreads = xreads.into_iter().collect();
    out.occ = beats as i64
        + if copies == 2 {
            hold(st, &operands, beats, core)
        } else {
            0
        };
    out
}
