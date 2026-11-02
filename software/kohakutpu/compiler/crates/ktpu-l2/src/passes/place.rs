//! `place` (L2.PASS.003): `par v in LO..HI on mg[any]` / `vc[any]` puts
//! instance i on unit (v - LO), mod the machine's count when the instances
//! outnumber the units or the count is not constant; a phase that wraps starts
//! on the unit after the last phase's last instance, so remainders rotate over
//! the units. A kind the machine has none of is refused.

use std::collections::BTreeMap;

use ktpu_l1::text::{IntStyle, Stmt, Term};

use super::{body_of, head, walk, write};
use crate::ir::{Program, Result};
use crate::target::Target;

pub fn place(p: &Program, t: &Target) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let r = p.module.reader();
    let mut next: BTreeMap<String, i128> = BTreeMap::new();
    let out = walk(&stmts, &mut |s: Stmt, _scopes: &[Stmt]| {
        if s.op != "par" {
            return Ok(vec![s]);
        }
        let pos = s.positional();
        let unit = match (pos.len(), pos.get(2)) {
            (3, Some(Term::View(n, sl))) if sl.len() == 1 && sl[0] == Term::name("any") => {
                n.clone()
            }
            _ => return Ok(vec![s]),
        };
        if unit != "mg" && unit != "vc" {
            return Err(r.fail(&s, "L2.PASS.003", format!("{unit}[any] is not a unit kind")));
        }
        let count = t.count(&unit);
        if count == 0 && !crate::mutant::on("l2.passes/any-on-absent-kind") {
            return Err(r.fail(
                &s,
                "L2.PASS.003",
                format!(
                    "{unit}[any]: this machine has no {} units",
                    unit.to_uppercase()
                ),
            ));
        }
        let Some((var, lo, hi)) = head(&s) else {
            return Ok(vec![s]);
        };
        let var = Term::name(var);
        let mut idx = if lo == Term::Int(0, IntStyle::Dec) {
            var
        } else {
            Term::BinOp('-', Box::new(var), Box::new(lo.clone()))
        };
        let n = match (&lo, &hi) {
            (Term::Int(l, _), Term::Int(h, _)) => Some((h - l) as i64),
            _ => None,
        };
        if n.map(|n| n > count).unwrap_or(true) {
            let units = count as i128;
            let first = next.entry(unit.clone()).or_insert(0);
            if *first != 0 {
                idx = Term::BinOp('+', Box::new(idx), Box::new(Term::int(*first)));
            }
            idx = Term::BinOp('%', Box::new(idx), Box::new(Term::int(units)));
            *first = (*first + n.unwrap_or(0) as i128) % units;
        }
        let mut out = s.clone();
        let mut args: Vec<Term> = s.args.iter().take(2).cloned().collect();
        args.push(Term::View(unit, vec![idx]));
        out.args = args;
        Ok(vec![out])
    })?;
    write(p, &params, &out)
}
