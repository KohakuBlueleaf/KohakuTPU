//! Expression emission: TG.PLAN.006, .015-.017, TG.FLT.003, .005.

use super::{card, floats, value_ops};
use ktpu_graph as g;
use ktpu_kernels::expr::{self, Operand, RedOp, E};
use ktpu_kernels::gen::vector::{self as v, Operand as Read};
use ktpu_kernels::Kind;

fn l(i: usize) -> Operand {
    expr::load(i).into()
}

fn op(name: &str, args: Vec<Operand>) -> E {
    expr::op(name, args).unwrap()
}

/// The math ops `e` emits over two f16 operands (loads, unpacks and the
/// store's pack aside).
fn math(e: &E) -> Vec<&'static str> {
    let k = v::flat_kernel(e, &[g::Dtype::F16; 3], 128, g::Dtype::F16, &[], &card()).unwrap();
    value_ops(&k)
        .into_iter()
        .filter(|o| !matches!(*o, "load" | "unpack" | "pack"))
        .collect()
}

#[test]
fn tg_plan_006_alu_ops() {
    assert_eq!(math(&op("add", vec![l(0), l(1)])), vec!["add"]);
    assert_eq!(math(&op("mul", vec![l(0), l(1)])), vec!["mul"]);
    assert_eq!(math(&op("recip", vec![l(0)])), vec!["inv"]);
    assert_eq!(math(&op("exp2", vec![l(0)])), vec!["exp2"]);
    assert_eq!(math(&op("log2", vec![l(0)])), vec!["log2"]);
    assert_eq!(math(&op("rsqrt", vec![l(0)])), vec!["rsqrt"]);
    assert_eq!(math(&op("where", vec![l(0), l(1), l(2)])), vec!["sel"]);
}

#[test]
fn tg_plan_015_more_alu() {
    assert_eq!(math(&op("max", vec![l(0), l(1)])), vec!["max"]);
    assert_eq!(math(&op("sqrt", vec![l(0)])), vec!["max", "rsqrt", "mul"]);
    assert_eq!(math(&op("cmplt", vec![l(0), l(1)])), vec!["cmplt"]);
    assert_eq!(math(&op("and", vec![l(0), l(1)])), vec!["mul"]);
    let sin = math(&op("sin", vec![l(0)]));
    assert_eq!(sin[..3], ["mul", "add", "sub"]);
    assert_eq!(sin.iter().filter(|o| **o == "mul").count(), 10);
}

#[test]
fn tg_plan_016_rest_of_the_alu() {
    assert_eq!(math(&op("sub", vec![l(0), l(1)])), vec!["sub"]);
    assert_eq!(math(&op("div", vec![l(0), l(1)])), vec!["inv", "mul"]);
    assert_eq!(math(&op("neg", vec![l(0)])), vec!["neg"]);
    assert_eq!(
        math(&op("pow", vec![l(0), l(1)])),
        vec!["log2", "mul", "exp2"]
    );
    assert_eq!(math(&op("cmpne", vec![l(0), l(1)])), vec!["cmpeq", "sub"]);
    assert_eq!(math(&op("cmpeq", vec![l(0), l(1)])), vec!["cmpeq"]);
    assert_eq!(math(&op("fma", vec![l(0), l(1), l(2)])), vec!["fma"]);
    // A constant condition picks its arm: no select.
    assert_eq!(
        math(&op("where", vec![1.0.into(), l(0), l(1)])),
        Vec::<&str>::new()
    );
    let k = v::flat_kernel(
        &op("div", vec![l(0), 4.0.into()]),
        &[g::Dtype::F16],
        128,
        g::Dtype::F16,
        &[],
        &card(),
    )
    .unwrap();
    assert_eq!(floats(&k), vec![0.25]);
}

#[test]
fn tg_plan_017_product_reduction() {
    let e = expr::reduce(RedOp::Prod, expr::load(0));
    let (k, _) = v::rows_kernel(
        &e,
        &[Read::full(64, 16)],
        16,
        64,
        &Read::new(Kind::Row),
        None,
        &card(),
    )
    .unwrap();
    let ops = value_ops(&k);
    assert_eq!(ops.iter().filter(|o| **o == "rsum").count(), 3);
    assert!(ops.contains(&"log2") && ops.contains(&"exp2") && ops.contains(&"min"));
}

#[test]
fn tg_flt_003_bools_are_lanes() {
    assert_eq!(math(&op("cmpgt", vec![l(0), l(1)])), vec!["cmpgt"]);
    // A number first is the mirrored compare.
    let k = v::flat_kernel(
        &op("cmplt", vec![0.5.into(), l(0)]),
        &[g::Dtype::F16],
        128,
        g::Dtype::F16,
        &[],
        &card(),
    )
    .unwrap();
    assert_eq!(math(&op("cmplt", vec![0.5.into(), l(0)])), vec!["cmpgt"]);
    assert_eq!(floats(&k), vec![0.5]);
}

#[test]
fn tg_flt_005_or_xor_not() {
    assert_eq!(math(&op("or", vec![l(0), l(1)])), vec!["max"]);
    assert_eq!(math(&op("xor", vec![l(0), l(1)])), vec!["sub", "abs"]);
    assert_eq!(math(&op("not", vec![l(0)])), vec!["sub"]);
}

/// `x - mean` times `mean` holds one reduction; two equal reductions built
/// apart are two (`application/tests/ktpugrad/test_lower.py`).
pub fn reduces_facts() -> (usize, usize) {
    let m = expr::reduce(RedOp::Sum, expr::load(0));
    let d = op("sub", vec![l(1), m.clone().into()]);
    let e = op("mul", vec![d.into(), m.clone().into()]);
    let two = op(
        "add",
        vec![m.into(), expr::reduce(RedOp::Sum, expr::load(0)).into()],
    );
    (expr::reduces(&e).len(), expr::reduces(&two).len())
}

#[test]
fn a_reduction_read_twice_is_one_reduction() {
    assert_eq!(reduces_facts(), (1, 2));
}

#[test]
fn shared_node_emitted_once_constants_folded() {
    let s = op("exp2", vec![l(0)]);
    let e = op(
        "add",
        vec![s.clone().into(), op("mul", vec![s.into(), l(1)]).into()],
    );
    assert_eq!(math(&e), vec!["exp2", "mul", "add"]);
    let k = v::flat_kernel(
        &op("add", vec![l(0), op("sin", vec![1.0.into()]).into()]),
        &[g::Dtype::F16],
        128,
        g::Dtype::F16,
        &[],
        &card(),
    )
    .unwrap();
    assert_eq!(floats(&k), vec![1f64.sin()]);
}
