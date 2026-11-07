//! `share` (L2.PASS.005): in a body over several nodes, a cluster fill whose
//! view and execution scopes are uniform across the cluster `par` becomes
//! `share=auto`, except a B fill past `mg.shared_b_max` entries.
//! Lowering chooses the groups from the instantiated physical participants.

use ktpu_l1::text::{Stmt, Term};

use super::{body_of, head, names, unit_of, walk, write};
use crate::ir::{Program, Result};
use crate::target::Target;

pub fn share(p: &Program, t: &Target) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let nodes = stmts
        .iter()
        .any(|s| s.op == "par" && unit_of(s).as_deref() == Some("node"));
    if !nodes {
        return Ok(p.clone());
    }
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        if s.op != "fill" || s.kw("share").is_some() {
            return Ok(vec![s]);
        }
        let Some(par) = scopes
            .iter()
            .rposition(|p| p.op == "par" && unit_of(p).as_deref() == Some("mg"))
        else {
            return Ok(vec![s]);
        };
        let var = head(&scopes[par]).map(|h| h.0).unwrap_or_default();
        let view = s.positional()[0].clone();
        if names(&[view]).contains(&var)
            || scopes[par + 1..]
                .iter()
                .any(|scope| names(&scope.args).contains(&var))
        {
            return Ok(vec![s]);
        }
        if let Some(Term::Placed(x, space)) = &s.annot {
            if let (Term::View(_, dims), Term::Name(sp)) = (x.as_ref(), space.as_ref()) {
                if sp == "B" {
                    let d: Vec<i64> = dims
                        .iter()
                        .map(|d| d.as_int().unwrap_or(0) as i64)
                        .collect();
                    let (rows, k) = (
                        d.first().copied().unwrap_or(0),
                        d.get(1).copied().unwrap_or(0),
                    );
                    if t.entries(rows, k) > t.shared_b_max {
                        return Ok(vec![s]);
                    }
                }
            }
        }
        let mut out = s.clone();
        out.args.push(Term::Assign(
            Box::new(Term::name("share")),
            Box::new(Term::name("auto")),
        ));
        Ok(vec![out])
    })?;
    write(p, &params, &out)
}
