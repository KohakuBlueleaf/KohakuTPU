//! `language/tests/test_syntax.py`: terms parse to their node and print
//! back, printed arithmetic keeps its value, statements carry their
//! position, and an error names its line and column.

use super::*;
use ktpu_l1::text::{fmt, parse, IntStyle, Stmt, Term};

pub const CHECKS: &[(&str, Check)] = &[
    (
        "test_syntax::test_a_term_parses_to_its_node_and_prints_back",
        a_term_parses_to_its_node_and_prints_back,
    ),
    (
        "test_syntax::test_printed_arithmetic_keeps_its_value",
        printed_arithmetic_keeps_its_value,
    ),
    (
        "test_syntax::test_a_statement_carries_target_args_annotation_block_and_position",
        a_statement_carries_its_position,
    ),
    (
        "test_syntax::test_an_error_names_its_line_and_column",
        an_error_names_its_line_and_column,
    ),
];

fn one(text: &str) -> Result<Stmt, String> {
    let mut got = parse(text, "<t>").map_err(|e| e.to_string())?;
    match got.len() {
        1 => Ok(got.remove(0)),
        n => Err(format!("{n} statements in {text:?}")),
    }
}

fn name(s: &str) -> Term {
    Term::Name(s.into())
}

fn int(v: i128) -> Term {
    Term::Int(v, IntStyle::Dec)
}

fn b(t: Term) -> Box<Term> {
    Box::new(t)
}

/// Every term form: the node it parses to, and its print back.
fn a_term_parses_to_its_node_and_prints_back() -> Result<(), String> {
    let inf = f64::INFINITY;
    let cases: Vec<(&str, Term)> = vec![
        ("x", name("x")),
        ("reduce.max", name("reduce.max")),
        ("42", int(42)),
        ("0x8010_0000", Term::Int(0x8010_0000, IntStyle::Hex)),
        ("16K", Term::Int(16384, IntStyle::Size)),
        ("2M", Term::Int(2 << 20, IntStyle::Size)),
        ("1.5", Term::Float(1.5)),
        ("1e-05", Term::Float(1e-05)),
        ("64x64x2", Term::Dims(vec![64, 64, 2])),
        ("\"kohakutpu-l1\"", Term::Str("kohakutpu-l1".into())),
        ("-inf", Term::Neg(b(Term::Float(inf)))),
        ("inf", Term::Float(inf)),
        ("info", name("info")),
        ("nanos", name("nanos")),
        ("!p", Term::Not(b(name("p")))),
        (
            "buf+0x40",
            Term::Offset(b(name("buf")), b(Term::Int(0x40, IntStyle::Hex))),
        ),
        ("0..H", Term::Span(b(int(0)), b(name("H")))),
        ("i..j", Term::Span(b(name("i")), b(name("j")))),
        (
            "tiles(L, bq)",
            Term::Call("tiles".into(), vec![name("L"), name("bq")]),
        ),
        (
            "f(x: f16[N], n=2)",
            Term::Call(
                "f".into(),
                vec![
                    Term::Typed("x".into(), b(Term::View("f16".into(), vec![name("N")]))),
                    Term::Kw("n".into(), b(int(2))),
                ],
            ),
        ),
        (
            "q[h, i, :]",
            Term::View(
                "q".into(),
                vec![name("h"), name("i"), Term::Slice(None, None)],
            ),
        ),
        (
            "m[:, *]",
            Term::View("m".into(), vec![Term::Slice(None, None), Term::NewAxis]),
        ),
        (
            "x[2 :]",
            Term::View("x".into(), vec![Term::Slice(Some(b(int(2))), None)]),
        ),
        (
            "x[: 8]",
            Term::View("x".into(), vec![Term::Slice(None, Some(b(int(8))))]),
        ),
        (
            "x[lo : hi]",
            Term::View(
                "x".into(),
                vec![Term::Slice(Some(b(name("lo"))), Some(b(name("hi"))))],
            ),
        ),
        ("x[]", Term::View("x".into(), vec![])),
        ("(1, 0)", Term::Tuple(vec![int(1), int(0)])),
        ("(x,)", Term::Tuple(vec![name("x")])),
        ("()", Term::Tuple(vec![])),
    ];
    for (src, term) in cases {
        let st = one(&format!("op {src}\n"))?;
        eq((src, st.args.clone()), (src, vec![term.clone()]))?;
        eq((src, fmt(&term)), (src, src.to_string()))?;
    }
    Ok(())
}

fn value(t: &Term, env: &[(&str, i128)]) -> Result<i128, String> {
    Ok(match t {
        Term::Int(v, _) => *v,
        Term::Name(n) => env.iter().find(|(k, _)| k == n).ok_or(format!("no {n}"))?.1,
        Term::Offset(a, c) => value(a, env)? + value(c, env)?,
        Term::BinOp(op, a, c) => {
            let (x, y) = (value(a, env)?, value(c, env)?);
            match op {
                '-' => x - y,
                '*' => x * y,
                '/' => x.div_euclid(y),
                '%' => x.rem_euclid(y),
                _ => x + y,
            }
        }
        other => return Err(format!("not arithmetic: {other:?}")),
    })
}

/// Reprinting a sum keeps its value: parentheses stay where they matter.
fn printed_arithmetic_keeps_its_value() -> Result<(), String> {
    let sources = [
        "8*(t % 2)",
        "64*(t/2) + 32*(t % 2)",
        "a - (b - c)",
        "a - (b + c)",
        "a + (b - c)",
        "(a - b) - c",
        "a*(b*c)",
        "a/(b*c)",
        "a % (b % c)",
        "(a*b) % c",
    ];
    let envs: [&[(&str, i128)]; 2] = [
        &[("t", 3), ("a", 17), ("b", 5), ("c", 3)],
        &[("t", 6), ("a", 40), ("b", 7), ("c", 4)],
    ];
    for src in sources {
        let t = one(&format!("op {src}\n"))?.args[0].clone();
        let again = one(&format!("op {}\n", fmt(&t)))?.args[0].clone();
        for env in envs {
            eq((src, value(&again, env)?), (src, value(&t, env)?))?;
        }
    }
    Ok(())
}

/// A statement carries its target, op, arguments, annotation, block, and
/// line and column (comments and blank lines counted).
fn a_statement_carries_its_position() -> Result<(), String> {
    let text = "\n# a comment\nm = carry -inf : f32[bq]\nmap h in 0..H, i in tiles(L, bq)\n    store o[h] = x\n";
    let got = parse(text, "<t>").map_err(|e| e.to_string())?;
    let (a, m) = (&got[0], &got[1]);
    eq(
        (
            a.target.clone(),
            a.op.as_str(),
            a.args.clone(),
            a.annot.clone(),
        ),
        (
            Some(name("m")),
            "carry",
            vec![Term::Neg(b(Term::Float(f64::INFINITY)))],
            Some(Term::View("f32".into(), vec![name("bq")])),
        ),
    )?;
    eq((a.line, a.col), (3, 1))?;
    eq(
        m.args.clone(),
        vec![
            Term::Member(b(name("h")), b(Term::Span(b(int(0)), b(name("H"))))),
            Term::Member(
                b(name("i")),
                b(Term::Call("tiles".into(), vec![name("L"), name("bq")])),
            ),
        ],
    )?;
    let st = &m.body[0];
    eq(
        st.args.clone(),
        vec![Term::Assign(
            b(Term::View("o".into(), vec![name("h")])),
            b(name("x")),
        )],
    )?;
    eq((st.line, st.col), (5, 5))
}

/// A syntax error names its line and column. For a text ending early (inside
/// a bracket) L1.TXT.014 fixes no column: the error is on its line, at or
/// after the last token (Python said 8, Rust says 10; lead's ruling).
fn an_error_names_its_line_and_column() -> Result<(), String> {
    for (text, at) in [
        ("a = add x,, y\n", (1, 11)),
        ("level l1\n  bad indent\n x\n", (3, 2)),
        ("op $\n", (1, 4)),
    ] {
        match parse(text, "<t>") {
            Ok(s) => return Err(format!("{text:?} parsed: {s:?}")),
            Err(e) => eq((text, (e.line, e.col)), (text, at))?,
        }
    }
    let early = "op (1, 2\n";
    let last = early.find('2').unwrap() as u32 + 1;
    match parse(early, "<t>") {
        Ok(s) => Err(format!("{early:?} parsed: {s:?}")),
        Err(e) if e.line == 1 && e.col >= last => Ok(()),
        Err(e) => Err(format!("{early:?}: line {} column {}", e.line, e.col)),
    }
}
