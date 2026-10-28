//! More reads than a core walks (`run._narrow`, TG.RUN.011, .026): the
//! reduction-free subexpression reading the most reads that fit,
//! materialised first into an f16 temporary, read back as one.

use std::rc::Rc;

use ktpu_machine::Refusal;

use super::rows::{all_reduces, prune, rebuild, reductions_of};
use super::{Bufs, Runner};
use crate::exec::View;
use crate::expr::{self, Expr, E};
use crate::lower::{Access, Plan};
use crate::uop::Id;

impl Runner<'_> {
    pub(super) fn narrow(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let most = self.max_reads();
        let e = plan.expr.as_ref().unwrap();
        let mut best: Option<(usize, E)> = None;
        let mut stack = vec![e.clone()];
        while let Some(x) = stack.pop() {
            match x.as_ref() {
                Expr::Op(_, args) => {
                    let n = expr::loads(&x).len();
                    let better = best.as_ref().is_none_or(|(b, _)| n > *b);
                    if (2..=most).contains(&n) && all_reduces(&x).is_empty() && better {
                        best = Some((n, x.clone()));
                    }
                    stack.extend(args.iter().cloned());
                }
                Expr::Reduce(_, a) => stack.push(a.clone()),
                _ => {}
            }
        }
        let Some((_, best)) = best else {
            return Err(Refusal::new(
                "TG.RUN.026",
                format!(
                    "{} reads with no part of at most {most}",
                    plan.accesses.len()
                ),
            ));
        };
        let (sub_e, sub_acc, _) = prune(&best, &plan.accesses);
        let live: Vec<Id> = sub_acc
            .iter()
            .flat_map(|a| a.coef.iter().map(|(r, _)| *r))
            .filter(|r| self.extent(*r) > 1)
            .collect();
        let rs: Vec<Id> = plan
            .out_ranges
            .iter()
            .copied()
            .chain(self.reduce_order(plan))
            .filter(|r| live.contains(r))
            .collect();
        let key = self.key("narrow");
        let nbytes = self.hw.f16_bytes(self.size(&rs));
        let tmp = self.x.storage(nbytes);
        let sub = Plan {
            name: format!("{}_n", plan.name),
            out_slot: key.clone(),
            out_dtype: "half".into(),
            out_shape: rs.iter().map(|r| self.extent(*r)).collect(),
            out_ranges: rs.clone(),
            expr: Some(sub_e),
            accesses: sub_acc,
            reduce_ranges: vec![],
            reductions: vec![],
            ..plan.clone()
        };
        let read = Access {
            slot: key.clone(),
            dtype: "half".into(),
            coef: self.dense(&rs),
            off: 0,
            table: None,
            gate: false,
        };
        let nacc = plan.accesses.len();
        let mut reds: Vec<(E, Vec<Id>)> = Vec::new();
        let swapped = rebuild(
            e,
            &mut |x| Rc::ptr_eq(x, &best).then(|| expr::load(nacc)),
            &mut |x, new| {
                if matches!(x.as_ref(), Expr::Reduce(..)) {
                    reds.push((new.clone(), reductions_of(plan, x)));
                }
                new
            },
        );
        let mut accs = plan.accesses.clone();
        accs.push(read);
        let (outer_e, outer_acc, remap) = prune(&swapped, &accs);
        let reductions = remap
            .iter()
            .map(|(k, v)| {
                let rs = &reds.iter().find(|(r, _)| Rc::ptr_eq(r, v)).unwrap().1;
                (k.clone(), rs.clone())
            })
            .collect();
        let outer = Plan {
            expr: Some(outer_e),
            accesses: outer_acc,
            reductions,
            ..plan.clone()
        };
        let mut sb = bufs.clone();
        sb.insert(
            key,
            View {
                base: tmp.clone(),
                offset: 0,
                nbytes,
            },
        );
        let got = self
            .vector(&sub, &sb)
            .and_then(|_| self.vector(&outer, &sb));
        self.x.free(&tmp);
        got
    }
}
