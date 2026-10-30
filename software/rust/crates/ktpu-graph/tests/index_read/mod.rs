//! An index's value two ways: from the graph's form, and from its printed
//! text read by the L2 grammar's precedence (`ktpu.pest`: `+ -` below
//! `* / %`, each left to right; `/` and `%` floor). The reader is written
//! here, apart from the printers, so it witnesses what the text says.
// Each test binary that includes this module uses part of it.
#![allow(dead_code)]

use ktpu_graph::{Atom, Index};

/// `x` at the variables' values; None where a divisor is 0.
pub fn value(x: &Index, env: &[(String, i64)]) -> Option<i64> {
    match x {
        Index::Int(n) => Some(*n),
        Index::Sym(s) => {
            let mut out = s.konst;
            for (a, c) in &s.terms {
                let v = match a {
                    Atom::Var(v) => env.iter().find(|(n, _)| n == v)?.1,
                    Atom::Floor(a, b) => value(a, env)?.checked_div_euclid(value(b, env)?)?,
                    Atom::Mod(a, b) => value(a, env)?.checked_rem_euclid(value(b, env)?)?,
                    Atom::Mul(a, b) => value(a, env)? * value(b, env)?,
                };
                out += c * v;
            }
            Some(out)
        }
    }
}

/// Every variable `x` names.
pub fn vars(x: &Index, out: &mut Vec<String>) {
    if let Index::Sym(s) = x {
        for (a, _) in &s.terms {
            match a {
                Atom::Var(v) if !out.contains(v) => out.push(v.clone()),
                Atom::Var(_) => {}
                Atom::Floor(a, b) | Atom::Mod(a, b) | Atom::Mul(a, b) => {
                    vars(a, out);
                    vars(b, out);
                }
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Int(i64),
    Name(String),
    Op(char),
}

fn tokens(s: &str) -> Vec<Tok> {
    let cs: Vec<char> = s.chars().collect();
    let mut out = vec![];
    let mut i = 0;
    while i < cs.len() {
        let c = cs[i];
        if c.is_whitespace() {
            i += 1;
        } else if c.is_ascii_digit() {
            let mut v = 0;
            while i < cs.len() && cs[i].is_ascii_digit() {
                v = v * 10 + i64::from(cs[i].to_digit(10).unwrap());
                i += 1;
            }
            out.push(Tok::Int(v));
        } else if c.is_ascii_alphabetic() || c == '_' {
            let start = i;
            while i < cs.len() && (cs[i].is_ascii_alphanumeric() || cs[i] == '_') {
                i += 1;
            }
            out.push(Tok::Name(cs[start..i].iter().collect()));
        } else {
            out.push(Tok::Op(c));
            i += 1;
        }
    }
    out
}

/// The text's value; None where a divisor is 0.
pub fn read(s: &str, env: &[(String, i64)]) -> Option<i64> {
    let t = tokens(s);
    let mut at = 0;
    let v = sum(&t, &mut at, env);
    assert_eq!(at, t.len(), "unread text in {s}");
    v
}

fn sum(t: &[Tok], at: &mut usize, env: &[(String, i64)]) -> Option<i64> {
    let mut acc = product(t, at, env)?;
    while let Some(Tok::Op(c @ ('+' | '-'))) = t.get(*at) {
        *at += 1;
        let rhs = product(t, at, env)?;
        acc = if *c == '+' { acc + rhs } else { acc - rhs };
    }
    Some(acc)
}

fn product(t: &[Tok], at: &mut usize, env: &[(String, i64)]) -> Option<i64> {
    let mut acc = unary(t, at, env)?;
    while let Some(Tok::Op(c @ ('*' | '/' | '%'))) = t.get(*at) {
        *at += 1;
        let rhs = unary(t, at, env)?;
        acc = match c {
            '*' => acc * rhs,
            '/' => acc.checked_div_euclid(rhs)?,
            _ => acc.checked_rem_euclid(rhs)?,
        };
    }
    Some(acc)
}

fn unary(t: &[Tok], at: &mut usize, env: &[(String, i64)]) -> Option<i64> {
    let tok = t[*at].clone();
    *at += 1;
    match tok {
        Tok::Op('-') => Some(-unary(t, at, env)?),
        Tok::Op('(') => {
            let v = sum(t, at, env);
            assert_eq!(t[*at], Tok::Op(')'));
            *at += 1;
            v
        }
        Tok::Int(v) => Some(v),
        Tok::Name(n) => Some(env.iter().find(|(m, _)| *m == n).unwrap().1),
        Tok::Op(c) => panic!("unexpected {c}"),
    }
}
