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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_use_mask_fuses_but_live_mask_and_inplace_payload_do_not() {
        let make = |dst: &str| {
            vec![
                event(ir::math(
                    "vcmpeq",
                    Term::name("p1"),
                    vec![Term::name("%1"), Term::name("k1")],
                    vec![],
                )),
                event(ir::math(
                    "vmov",
                    Term::name("%2"),
                    vec![Term::name("k0")],
                    vec![],
                )),
                event(ir::math(
                    "vmov",
                    Term::name("%2"),
                    vec![Term::name("k1")],
                    vec![ir::kw("if", Term::name("p1"))],
                )),
                event(ir::math(
                    "vsel",
                    Term::name(dst),
                    vec![Term::name("%3"), Term::name("k0"), Term::name("%2")],
                    vec![],
                )),
            ]
        };
        let fused = fuse(make("%4"));
        assert_eq!(fused.len(), 3);
        assert_eq!(op(&fused[2]).unwrap().kw("if"), Some(&Term::name("p1")));
        assert_eq!(fuse(make("%3")).len(), 4);
        let mut live = make("%4");
        live.push(event(ir::math(
            "vmov",
            Term::name("%5"),
            vec![Term::name("%2")],
            vec![],
        )));
        assert_eq!(fuse(live).len(), 5);
        let mut constants = make("%4");
        constants[0] = Event::Op {
            stmt: ir::math(
                "vcmpeq",
                Term::name("p1"),
                vec![Term::name("k0"), Term::name("k1")],
                vec![],
            ),
            syms: vec![Symbol(2)],
        };
        let fused = fuse(constants);
        assert!(matches!(&fused[1], Event::Op { syms, .. } if syms == &vec![Symbol(2)]));
    }

    #[cfg(feature = "mutants")]
    #[test]
    fn swapped_arms_reach_the_fused_select() {
        let sel = vec![
            event(ir::math(
                "vcmplt",
                Term::name("p1"),
                vec![Term::name("%1"), Term::name("%5")],
                vec![],
            )),
            event(ir::math(
                "vmov",
                Term::name("%2"),
                vec![Term::name("k0")],
                vec![],
            )),
            event(ir::math(
                "vmov",
                Term::name("%2"),
                vec![Term::name("k1")],
                vec![ir::kw("if", Term::name("p1"))],
            )),
            event(ir::math(
                "vsel",
                Term::name("%4"),
                vec![Term::name("%1"), Term::name("%5"), Term::name("%2")],
                vec![],
            )),
        ];
        let sources = |events: Vec<Event>| -> Vec<Term> {
            [0, 2]
                .iter()
                .map(|&i| op(&events[i]).unwrap().positional()[1].clone())
                .collect()
        };
        assert_eq!(
            sources(fuse(sel.clone())),
            [Term::name("%5"), Term::name("%1")]
        );
        let swapped = crate::mutant::State {
            active: Some("l2.vector/sel-arms-swapped"),
            ..Default::default()
        };
        assert_eq!(
            crate::mutant::scoped(swapped, || sources(fuse(sel))),
            [Term::name("%1"), Term::name("%5")]
        );
    }
}
