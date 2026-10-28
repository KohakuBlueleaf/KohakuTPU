//! Casts add lane operations only where the value changes.
use super::*;

fn cast_expr(boolean_cast: bool) -> E {
    let mut g = Graph::default();
    let x = g.make("PARAM", "half", vec![], Arg::Int(0));
    let root = if boolean_cast {
        let b = g.cast(x, "bool");
        g.cast(b, "half")
    } else {
        let zero = g.konst(&Arg::Int(0), "half");
        let b = g.binop("CMPLT", zero, x);
        let h = g.cast(b, "half");
        g.binop("MUL", h, x)
    };
    let mut reader = Reader {
        out_ranges: vec![],
        accesses: vec![],
        reduce_ranges: vec![],
        reductions: vec![],
        memo: HashMap::from([(x, expr::load(0))]),
        imemo: HashMap::new(),
    };
    reader.read(&mut g, root).unwrap()
}

#[test]
fn a_cast_costs_ops_only_where_the_value_changes() {
    let x = expr::load(0);
    assert_eq!(
        cast_expr(true),
        expr::op("cmpne", vec![x.clone(), expr::constant(0.0)])
    );
    assert_eq!(
        cast_expr(false),
        expr::op(
            "mul",
            vec![expr::op("cmplt", vec![expr::constant(0.0), x.clone()]), x]
        )
    );
}

#[cfg(feature = "mutants")]
#[test]
fn redundant_cast_operations_are_detected() {
    for b in [true, false] {
        let want = cast_expr(b);
        assert_ne!(mutant::with("tg.lower/cast_scaled", || cast_expr(b)), want);
    }
}
