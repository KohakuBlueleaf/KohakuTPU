//! `split` (L2.PASS.006): `c = gemm a, b[, c] split` after its two fills
//! becomes a loop over K in chunks of the operands' packing, fills and sweep
//! inside, a `zero` before when no accumulator is named.

use ktpu_l1::text::{Reader, Stmt, Term};

use super::{body_of, head, walk, write};
use crate::ir::{Buffer, Program, Result};
use crate::target::Target;

fn int(t: &Term) -> i64 {
    t.as_int().unwrap_or(0) as i64
}

#[allow(clippy::too_many_arguments)]
fn split_one(
    r: &Reader,
    gemm: &Stmt,
    fa: &Stmt,
    fb: &Stmt,
    params: &[(String, Term)],
    scopes: &[Stmt],
    t: &Target,
) -> Result<Vec<Stmt>> {
    let (Some(Term::Placed(ma, _)), Some(Term::Placed(nb, _))) = (&fa.annot, &fb.annot) else {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            "gemm ... split follows the two fills it reads",
        ));
    };
    let (Term::View(_, md), Term::View(_, nd)) = (ma.as_ref(), nb.as_ref()) else {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            "gemm ... split follows the two fills it reads",
        ));
    };
    let (rows_a, k) = (int(&md[0]), int(&md[1]));
    let rows_b = int(&nd[0]);
    let view_a = fa.positional()[0].clone();
    let view_b = fb.positional()[0].clone();
    let Term::View(an, _) = &view_a else {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            "gemm ... split follows the two fills it reads",
        ));
    };
    let ty = params.iter().find(|(n, _)| n == an).map(|(_, t)| t.clone());
    let Some(ty) = ty else {
        return Err(r.fail(gemm, "L2.PASS.006", format!("split: {an} is no parameter")));
    };
    let buf = Buffer::of(&ty).map_err(|e| r.fail(gemm, "L2.PASS.006", e))?;
    let chunk = buf.get("nk") * t.kblock;
    if k % chunk != 0 || k == chunk {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            format!("split: K = {k} is not several packed chunks of {chunk}"),
        ));
    }
    if t.entries(rows_a.max(rows_b), chunk) > t.bank {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            "split: one K chunk of an operand still overflows a bank",
        ));
    }
    let used: Vec<String> = scopes
        .iter()
        .filter(|p| p.op == "for" || p.op == "par")
        .filter_map(|p| head(p).map(|h| h.0))
        .collect();
    let Some(var) = ["kk", "k2", "k3", "k4"]
        .iter()
        .find(|v| !used.contains(&v.to_string()))
    else {
        return Err(r.fail(
            gemm,
            "L2.PASS.006",
            "split: no loop variable left (kk, k2, k3, k4)",
        ));
    };
    let kv = Term::name(*var);
    let chunked = |view: &Term| -> Term {
        let Term::View(n, sl) = view else {
            return view.clone();
        };
        let (lead, ks) = sl.split_at(sl.len() - 1);
        let lo = match &ks[0] {
            Term::Slice(Some(lo), _) => (**lo).clone(),
            _ => Term::int(0),
        };
        let mut s = lead.to_vec();
        s.push(Term::Slice(
            Some(Box::new(Term::Offset(
                Box::new(lo),
                Box::new(Term::BinOp(
                    '*',
                    Box::new(kv.clone()),
                    Box::new(Term::int(chunk as i128)),
                )),
            ))),
            Some(Box::new(Term::Pos(Box::new(Term::int(chunk as i128))))),
        ));
        Term::View(n.clone(), s)
    };
    let typed = |t: &Term| -> Term {
        let Term::Placed(x, sp) = t else {
            return t.clone();
        };
        let Term::View(n, d) = x.as_ref() else {
            return t.clone();
        };
        Term::Placed(
            Box::new(Term::View(
                n.clone(),
                vec![d[0].clone(), Term::int(chunk as i128)],
            )),
            sp.clone(),
        )
    };
    let mut a = fa.clone();
    a.args = std::iter::once(chunked(&view_a))
        .chain(fa.args.iter().skip(1).cloned())
        .collect();
    a.annot = fa.annot.as_ref().map(typed);
    let mut b = fb.clone();
    b.args = std::iter::once(chunked(&view_b))
        .chain(fb.args.iter().skip(1).cloned())
        .collect();
    b.annot = fb.annot.as_ref().map(typed);
    let mut names: Vec<Term> = gemm
        .positional()
        .into_iter()
        .filter(|x| x.as_name() != Some("split"))
        .cloned()
        .collect();
    let mut first = Vec::new();
    if names.len() == 2 {
        let acc = gemm.target.clone().unwrap();
        let mut z = Stmt::new("zero", vec![]);
        z.target = Some(acc.clone());
        z.annot = gemm.annot.clone();
        z.line = gemm.line;
        first.push(z);
        names.push(acc);
    }
    let mut g = gemm.clone();
    g.args = names;
    let mut lp = Stmt::new(
        "for",
        vec![Term::Member(
            Box::new(kv.clone()),
            Box::new(Term::Span(
                Box::new(Term::int(0)),
                Box::new(Term::int((k / chunk) as i128)),
            )),
        )],
    );
    lp.body = vec![a, b, g];
    lp.line = gemm.line;
    first.push(lp);
    Ok(first)
}

pub fn split(p: &Program, t: &Target) -> Result<Program> {
    if crate::mutant::on("l2.passes/split-off") {
        return Ok(p.clone());
    }
    let (params, stmts) = body_of(p)?;
    let r = p.module.reader();
    let (params, r) = (&params, &r);
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        if s.body.is_empty() {
            return Ok(vec![s]);
        }
        let mut out: Vec<Stmt> = Vec::new();
        for st in &s.body {
            if st.op == "gemm" && st.positional().iter().any(|x| x.as_name() == Some("split")) {
                let n = out.len();
                if n < 2 || out[n - 2].op != "fill" || out[n - 1].op != "fill" {
                    return Err(r.fail(
                        st,
                        "L2.PASS.006",
                        "gemm ... split follows the two fills it reads",
                    ));
                }
                let fb = out.pop().unwrap();
                let fa = out.pop().unwrap();
                out.extend(split_one(r, st, &fa, &fb, params, scopes, t)?);
            } else {
                out.push(st.clone());
            }
        }
        let mut s = s;
        s.body = out;
        Ok(vec![s])
    })?;
    write(p, params, &out)
}
