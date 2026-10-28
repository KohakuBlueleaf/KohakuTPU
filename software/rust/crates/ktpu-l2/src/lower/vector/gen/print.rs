//! Vector-template spelling and source positions for constructed statements.

use ktpu_l1::text::{Definition, IntStyle, Module, Stmt, Term};

use super::typed::Result;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Rule {
    Legacy,
    #[cfg(test)]
    SpacedSlice,
    #[cfg(test)]
    TightBinding,
    #[cfg(test)]
    ReverseKeywords,
}

fn name(s: &str) -> Result<&str> {
    if !s.split('.').all(|part| {
        !part.is_empty()
            && !part.as_bytes()[0].is_ascii_digit()
            && part.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
    }) {
        return Err(format!("not a resolved generated name: {s:?}"));
    }
    Ok(s)
}

fn join(ts: &[Term], rule: Rule) -> Result<String> {
    Ok(ts
        .iter()
        .map(|t| term(t, rule))
        .collect::<Result<Vec<_>>>()?
        .join(", "))
}

fn term(t: &Term, rule: Rule) -> Result<String> {
    Ok(match t {
        Term::Name(n) => name(n)?.into(),
        Term::Int(n, IntStyle::Dec) if *n >= 0 => n.to_string(),
        Term::Float(f) if f.is_nan() || !f.is_sign_negative() => ktpu_l1::text::py_float(*f),
        Term::Neg(n) if matches!(n.as_ref(), Term::Int(..) | Term::Float(_)) => {
            format!("-{}", term(n, rule)?)
        }
        Term::View(n, sl) => format!("{}[{}]", name(n)?, join(sl, rule)?),
        Term::Slice(Some(lo), None) => {
            #[cfg(test)]
            if rule == Rule::SpacedSlice {
                return Ok(format!("{} :", term(lo, rule)?));
            }
            format!("{}:", term(lo, rule)?)
        }
        Term::Call(n, args) => format!("{}({})", name(n)?, join(args, rule)?),
        Term::Kw(n, value) => format!("{}={}", name(n)?, term(value, rule)?),
        Term::Assign(a, b) => format!("{}={}", term(a, rule)?, term(b, rule)?),
        Term::AddAssign(a, b) => format!("{} += {}", term(a, rule)?, term(b, rule)?),
        Term::Tuple(ts) if ts.len() == 1 => format!("({},)", term(&ts[0], rule)?),
        Term::Tuple(ts) => format!("({})", join(ts, rule)?),
        Term::Arrow(a) if matches!(a.as_str(), "<-" | "->") => a.clone(),
        _ => return Err(format!("unsupported generated term: {t:?}")),
    })
}

fn head(st: &Stmt, rule: Rule) -> Result<String> {
    if st.target.is_some() || st.annot.is_some() {
        return Err("generated instructions have no binding target or annotation".into());
    }
    let mut out = name(&st.op)?.to_string();
    let args: Vec<&Term> = st.args.iter().collect();
    #[cfg(test)]
    let args = if rule == Rule::ReverseKeywords {
        let mut args = args;
        let mut keywords: Vec<_> = args
            .iter()
            .copied()
            .filter(|a| a.as_kwarg().is_some())
            .collect();
        for a in &mut args {
            if a.as_kwarg().is_some() {
                *a = keywords.pop().unwrap();
            }
        }
        args
    } else {
        args
    };
    for (i, a) in args.iter().enumerate() {
        let prev = i.checked_sub(1).map(|p| args[p]);
        let comma = st.commas
            && i > 0
            && a.as_kwarg().is_none()
            && !matches!(a, Term::Arrow(_))
            && !matches!(prev, Some(Term::Arrow(_)));
        out.push_str(if comma { ", " } else { " " });
        if i == 0 && matches!(st.op.as_str(), "desc" | "seti") {
            let Term::Assign(lhs, rhs) = a else {
                return Err("desc/seti requires an assignment".into());
            };
            #[cfg(test)]
            if rule == Rule::TightBinding {
                out.push_str(&term(a, rule)?);
                continue;
            }
            out.push_str(&format!("{} = {}", term(lhs, rule)?, term(rhs, rule)?));
        } else {
            out.push_str(&term(a, rule)?);
        }
    }
    Ok(out)
}

fn lines(
    stmts: &mut [Stmt],
    depth: u32,
    line: &mut u32,
    out: &mut String,
    rule: Rule,
) -> Result<()> {
    for st in stmts {
        if !st.body.is_empty() && !matches!(st.op.as_str(), "image" | "loop" | "skipz") {
            return Err("unsupported generated block".into());
        }
        st.line = *line;
        st.col = 4 * depth + 1;
        out.push_str(&" ".repeat(4 * depth as usize));
        out.push_str(&head(st, rule)?);
        out.push('\n');
        *line += 1;
        lines(&mut st.body, depth + 1, line, out, rule)?;
    }
    Ok(())
}

/// Prints generated statements and assigns their one-based line and column.
pub fn emit(stmts: &mut [Stmt]) -> Result<String> {
    emit_rule(stmts, Rule::Legacy)
}

pub(super) fn emit_rule(stmts: &mut [Stmt], rule: Rule) -> Result<String> {
    let mut out = String::new();
    lines(stmts, 0, &mut 1, &mut out, rule)?;
    Ok(out)
}

/// A single-image module, with the defaults used by the search's image reader.
pub fn image(name: &str, params: Vec<String>, body: Vec<Stmt>, vregs: u32) -> Result<Module> {
    if body.is_empty() {
        return Err("an image has a nonempty body".into());
    }
    let mut header = Stmt::new(
        "image",
        vec![Term::Call(
            name.into(),
            params.iter().map(Term::name).collect(),
        )],
    );
    header.body = body;
    let source = emit(std::slice::from_mut(&mut header))?;
    let mut module = Module {
        source,
        file: format!("<{name}>"),
        vregs,
        ..Module::default()
    };
    module.images.insert(
        name.into(),
        Definition {
            kind: "image".into(),
            name: name.into(),
            params,
            stmts: header.body,
            line: header.line,
        },
    );
    Ok(module)
}
