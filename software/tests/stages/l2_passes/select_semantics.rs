use std::collections::HashMap;

use ktpu_isa::numerics::e8m15_value;
use ktpu_l1::text::{Stmt, Term};
use ktpu_l2::ir::Program;
use ktpu_l2::passes::select::select;

fn operand(t: &Term, values: &HashMap<String, f64>) -> f64 {
    match t {
        Term::Float(x) => *x,
        Term::Int(x, _) => *x as f64,
        Term::Name(n) if n == "nan" => f64::NAN,
        Term::Name(n) if n == "inf" => f64::INFINITY,
        Term::Name(n) => values[n],
        Term::Neg(x) => -operand(x, values),
        _ => panic!("unexpected operand {t:?}"),
    }
}

fn execute(body: &[Stmt], x: f64) -> f64 {
    let mut values = HashMap::from([("x".into(), x)]);
    for st in body {
        let args: Vec<_> = st
            .positional()
            .iter()
            .map(|t| operand(t, &values))
            .collect();
        let v = match st.op.as_str() {
            "mul" => {
                let product = args[0] * e8m15_value(args[1]);
                if product.is_nan() {
                    f64::NAN
                } else {
                    e8m15_value(product)
                }
            }
            "cmpgt" => {
                if args[0] > args[1] {
                    1.0
                } else {
                    0.0
                }
            }
            "cmplt" => {
                if args[0] < args[1] {
                    1.0
                } else {
                    0.0
                }
            }
            "sel" => {
                if args[0] != 0.0 {
                    args[1]
                } else {
                    args[2]
                }
            }
            "min" | "max" if args[0].is_nan() || args[1].is_nan() => args[0],
            "min" => {
                if args[0] > args[1] {
                    args[1]
                } else {
                    args[0]
                }
            }
            "max" => {
                if args[0] < args[1] {
                    args[1]
                } else {
                    args[0]
                }
            }
            op => panic!("unexpected operation {op}"),
        };
        let v = if v.is_nan() { f64::NAN } else { v };
        values.insert(st.target_name().expect("value").into(), v);
    }
    values["y"]
}

#[test]
fn select_preserves_exceptional_scaled_arm() {
    for (scale, x) in [
        (f64::NAN, -1.0),
        (f64::INFINITY, 0.0),
        (0.0, f64::NEG_INFINITY),
    ] {
        let mut p = Program::parse(
            "fn f.l2()\n    for i in 0..1\n        s = mul x, 0.5\n        m = cmpgt x, 0\n        y = sel m, x, s\n",
            "select",
            Some("f"),
        )
        .expect("program");
        let body = p
            .module
            .fns
            .get_mut(&("f".into(), "l2".into()))
            .expect("body");
        body.stmts[0].body[0].args[1] = Term::Float(scale);
        let out = select(&p).expect("pass");
        let body = &out.module.body("f", "l2").expect("body").stmts[0].body;
        assert!(
            execute(body, x).is_nan(),
            "select must choose the exceptional scaled arm"
        );
    }
}
