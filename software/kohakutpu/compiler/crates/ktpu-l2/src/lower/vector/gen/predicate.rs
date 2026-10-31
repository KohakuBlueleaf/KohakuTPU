//! Consume a one-use comparison mask as a predicate instead of materializing
//! 0/1 in RF and selecting it back. Payloads move by vmov, as VSEL would move
//! them: bit for bit except a zero, which comes back +0 (ISA.VC.058).

use std::collections::HashMap;

use ktpu_l1::text::{Stmt, Term};

use super::typed::{self as ir, Event, Symbol};
use crate::passes::names;

fn op(e: &Event) -> Option<&Stmt> {
    match e {
        Event::Op { stmt, .. } => Some(stmt),
        _ => None,
    }
}

fn event(st: Stmt) -> Event {
    // Allocator metadata is destination-first, not sorted textual occurrence.
    let mut ordered = names(&st.args[0..1]).into_iter().collect::<Vec<_>>();
    let rest = names(&st.args[1..]);
    ordered.extend(rest);
    let syms = ordered
        .into_iter()
        .filter_map(|n| n.strip_prefix('%').map(|n| Symbol(n.parse().unwrap())))
        .collect();
    Event::Op { stmt: st, syms }
}

pub fn fuse(events: Vec<Event>) -> Vec<Event> {
    if crate::mutant::on("l2.predicate-select/off") {
        return events;
    }
    let mut occurrences = HashMap::<String, usize>::new();
    for e in &events {
        if let Event::Op { stmt, .. } | Event::Fixed(stmt) = e {
            for n in names(&stmt.args) {
                *occurrences.entry(n).or_default() += 1;
            }
        }
    }
    let mut out = Vec::new();
    let mut i = 0;
    while i < events.len() {
        let found = (|| {
            let cmp = op(events.get(i)?)?;
            let zero = op(events.get(i + 1)?)?;
            let one = op(events.get(i + 2)?)?;
            let sel = op(events.get(i + 3)?)?;
            if !matches!(cmp.op.as_str(), "vcmpeq" | "vcmplt" | "vcmpgt")
                || zero.op != "vmov"
                || one.op != "vmov"
                || sel.op != "vsel"
            {
                return None;
            }
            let z = zero.positional();
            let o = one.positional();
            let s = sel.positional();
            if z.len() != 2 || o.len() != 2 || s.len() != 4 {
                return None;
            }
            let mask = z[0].as_name()?;
            if occurrences[mask] != 3
                || o[0] != z[0]
                || s[3] != z[0]
                || z[1] != &Term::name("k0")
                || o[1] != &Term::name("k1")
                || zero.kw("if").is_some()
                || one.kw("if") != Some(&Term::name("p1"))
                || names(&[s[1].clone(), s[2].clone()]).contains(mask)
                // An in-place destination cannot clobber a value still read
                // by the comparison or by the later true-arm write.
                || names(&cmp.args).contains(s[0].as_name()?)
                || names(&[s[1].clone()]).contains(s[0].as_name()?)
            {
                return None;
            }
            let (yes, no) = if crate::mutant::on("l2.vector/sel-arms-swapped") {
                (s[2], s[1])
            } else {
                (s[1], s[2])
            };
            let initial = ir::math("vmov", s[0].clone(), vec![no.clone()], vec![]);
            let arm = if crate::mutant::on("l2.predicate-select/wrong-arm") {
                no
            } else {
                yes
            };
            let chosen = ir::math(
                "vmov",
                s[0].clone(),
                vec![arm.clone()],
                vec![ir::kw("if", Term::name("p1"))],
            );
            Some([event(initial), events[i].clone(), event(chosen)])
        })();
        if let Some(seq) = found {
            out.extend(seq);
            i += 4;
        } else {
            out.push(events[i].clone());
            i += 1;
        }
    }
    out
}
