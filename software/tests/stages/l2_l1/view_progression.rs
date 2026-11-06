use ktpu_l1::text::Env;
use ktpu_l2::ir::{Node, Program};
use ktpu_l2::lower::vector::ctx::Ctx;
use ktpu_l2::lower::vector::VErr;
use ktpu_l2::target::Target;

use super::common::card;

#[test]
fn interior_trip_must_match_the_descriptor() {
    let p = Program::parse(
        "fn f.l2(A: f16[5,128])\n    for i in 0..4\n        x = load A[i+i*(i-1)*(3-i),:] : f16[1,128] @l1\n",
        "views",
        Some("f"),
    )
    .expect("program");
    let body = p.body().expect("body");
    let Node::For(lp) = body.stmts[0].as_ref() else {
        panic!("loop")
    };
    let target = Target::of(&card());
    let mut ctx = Ctx::new(&body, &target, &Env::new(), Some(lp.clone())).expect("context");
    let error = ctx
        .prepare(lp.body[0].as_ref(), true)
        .expect_err("rows 0,1,4,3 are not affine");
    match error {
        VErr::Fail(error) => assert!(error.to_string().contains("L2.VC.021")),
        other => panic!("expected affine-view refusal, got {other:?}"),
    }
}

#[test]
fn banded_unpack_checks_interior_rows() {
    let p = Program::parse(
        "fn f.l2(A: f16[5,128])\n    h = load A : f16[5,128] @l1\n    for i in 0..4\n        x = unpack h[i+i*(i-1)*(3-i),:] : f32[1,128] @reg\n",
        "bands",
        Some("f"),
    )
    .expect("program");
    let body = p.body().expect("body");
    let Node::For(lp) = body.stmts[1].as_ref() else {
        panic!("loop")
    };
    let target = Target::of(&card());
    let mut ctx = Ctx::new(&body, &target, &Env::new(), Some(lp.clone())).expect("context");
    ctx.pre.push(body.stmts[0].clone());
    let error = ctx
        .prepare(lp.body[0].as_ref(), true)
        .expect_err("a sequential pointer must not substitute row 2 for row 4");
    match error {
        VErr::Fail(error) => assert!(error.to_string().contains("L2.VC.009")),
        other => panic!("expected band progression refusal, got {other:?}"),
    }
}
