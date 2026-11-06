use ktpu_l1::text::{fmt, parse, Term};

fn eval(t: &Term, a: i128, b: i128, c: i128) -> i128 {
    match t {
        Term::Name(n) => match n.as_str() {
            "a" => a,
            "b" => b,
            "c" => c,
            _ => panic!("unknown input {n}"),
        },
        Term::Int(v, _) => *v,
        Term::Neg(x) => -eval(x, a, b, c),
        Term::Not(x) => i128::from(eval(x, a, b, c) == 0),
        Term::Pos(x) => eval(x, a, b, c),
        Term::Offset(x, y) | Term::BinOp('+', x, y) => eval(x, a, b, c) + eval(y, a, b, c),
        Term::BinOp('*', x, y) => eval(x, a, b, c) * eval(y, a, b, c),
        Term::Compare(op, x, y) if op == "<" => i128::from(eval(x, a, b, c) < eval(y, a, b, c)),
        _ => panic!("unexpected expression {t:?}"),
    }
}

#[test]
fn unary_operands_keep_their_value() {
    let sum = Term::BinOp('+', Box::new(Term::name("a")), Box::new(Term::name("b")));
    let compare = Term::Compare(
        "<".into(),
        Box::new(Term::name("a")),
        Box::new(Term::name("b")),
    );
    for (term, expected) in [
        (Term::Neg(Box::new(sum.clone())), -5),
        (Term::Not(Box::new(sum.clone())), 0),
        (
            Term::BinOp(
                '*',
                Box::new(Term::Pos(Box::new(compare))),
                Box::new(Term::name("c")),
            ),
            7,
        ),
        (
            Term::BinOp(
                '*',
                Box::new(Term::Pos(Box::new(sum))),
                Box::new(Term::name("c")),
            ),
            35,
        ),
    ] {
        let executable =
            parse(&format!("value {}", fmt(&term)), "printed").expect("printed expression");
        assert_eq!(eval(&executable[0].args[0], 2, 3, 7), expected);
    }
}
