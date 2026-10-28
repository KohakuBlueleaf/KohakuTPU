//! `place` (L2.PASS.003): `par v in LO..HI on mg[any]` / `vc[any]` puts
//! instance i on unit (v - LO), mod the machine's count when the instances
//! outnumber the units or the count is not constant. A kind the machine has
//! none of is refused.

use ktpu_l1::text::{IntStyle, Stmt, Term};

use super::{body_of, head, walk, write};
use crate::ir::{Program, Result};
use crate::target::Target;

pub fn place(p: &Program, t: &Target) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let r = p.module.reader();
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
            idx = Term::BinOp('%', Box::new(idx), Box::new(Term::int(count as i128)));
        }
        let mut out = s.clone();
        let mut args: Vec<Term> = s.args.iter().take(2).cloned().collect();
        args.push(Term::View(unit, vec![idx]));
        out.args = args;
        Ok(vec![out])
    })?;
    write(p, &params, &out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(feature = "mutants")]
    use crate::mutant::{scoped, State};
    use ktpu_machine::Machine;

    const ANY_VC: &str = concat!(
        "fn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n",
        "    par c in 0..2 on vc[any]\n",
        "        for t in 0..2\n",
        "            xl = load x[c*32 + t*16 : +16, :] : f16[16, 128] @l1\n",
        "    copy y <- x\n",
    );

    fn place_on(machine: &str) -> Result<Program> {
        let p = Program::parse(ANY_VC, "<t>", None).unwrap();
        place(&p, &Target::of(&Machine::named(machine).unwrap()))
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_PASS_003_any_on_absent_kind() {
        assert!(place_on("multimesh_v9t6").is_ok());
        let e = place_on("sru_v9t7").unwrap_err();
        assert_eq!(e.clause, "L2.PASS.003");
        assert_eq!(e.message, "vc[any]: this machine has no VC units");
    }

    /// The witness: without the refusal the clause test above fails.
    #[cfg(feature = "mutants")]
    #[test]
    fn mutant_any_on_absent_kind_is_caught() {
        let state = State {
            active: Some("l2.passes/any-on-absent-kind"),
            ..State::default()
        };
        let got = scoped(state, || place_on("sru_v9t7"));
        assert!(!matches!(got, Err(e) if e.clause == "L2.PASS.003"));
    }
}
