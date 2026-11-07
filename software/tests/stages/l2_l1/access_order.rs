//! Dependency ordering follows repeated descriptor bases and physical aliases.

use std::collections::BTreeMap;

use ktpu_l1::text::{read, Term};
use ktpu_l2::lower::events::BackendSyncFacts;
use ktpu_l2::lower::lower_bound;
use ktpu_l2::target::Target;
use ktpu_l2::{Program, Search};

use super::common::{card, verify_card_l1};

fn ordered(source: &str, bindings: BTreeMap<String, i64>) {
    let machine = card();
    let target = Target::of(&machine);
    let program =
        Program::parse(source, "access order", Some("ordered")).expect("vector dependency");
    let (text, _) = lower_bound(
        &program,
        &target,
        &Search::default(),
        &BackendSyncFacts::common(target.il_slots),
        &bindings,
        |_, _| Ok(()),
    )
    .expect("bound vector schedule");
    let module = read(&text, "access order").expect("lowered L1");
    let body = module.body("ordered", "l1").expect("schedule");
    let core = |stmt: &ktpu_l1::text::Stmt, index| {
        matches!(stmt.positional().first(), Some(Term::View(name, args))
            if name == "vc" && args.first().and_then(Term::as_int) == Some(index))
    };
    let producer = body
        .stmts
        .iter()
        .position(|s| s.op == "send" && core(s, 0))
        .expect("producer send");
    let consumer = body
        .stmts
        .iter()
        .position(|s| s.op == "send" && core(s, 1))
        .expect("consumer send");
    assert!(
        body.stmts[producer + 1..consumer]
            .iter()
            .any(|s| s.op == "wait" && core(s, 0)),
        "the reader must wait for its actual producer: {text}"
    );
    verify_card_l1(
        &text,
        "ordered",
        &machine,
        bindings
            .into_iter()
            .map(|(n, a)| (n, i128::from(a)))
            .collect(),
        &ktpu_verify::MemoryContext::default(),
    );
}

#[test]
fn second_repetition_orders_its_reader() {
    ordered(
        r#"target ktpu.v9
fn ordered.l2(A: f16[2,128] @dram, B: f16[2,128] @dram, out: f16[1,128] @dram)
    par c in 0..1 on vc[c] repeats=2 advances=(0,256)
        for t in 0..1
            h = load A[0,:] : f16[1,128] @l1
            v = unpack h : f32[1,128] @reg
            q = pack v : f16[1,128] @l1
            store B[0,:] <- q
    par c in 1..2 on vc[c]
        for t in 0..1
            h = load B[1,:] : f16[1,128] @l1
            v = unpack h : f32[1,128] @reg
            q = pack v : f16[1,128] @l1
            store out[0,:] <- q
"#,
        BTreeMap::from([
            ("A".into(), 4096),
            ("B".into(), 8192),
            ("out".into(), 12288),
        ]),
    );
}

#[test]
fn aliases_within_one_par_order_their_reader() {
    ordered(
        r#"target ktpu.v9
fn ordered.l2(src: f16[1,128] @dram, A: f16[1,128] @dram, B: f16[1,128] @dram, out: f16[1,128] @dram)
    par c in 0..2 on vc[c]
        when c == 0
            for t in 0..1
                h = load src[0,:] : f16[1,128] @l1
                v = unpack h : f32[1,128] @reg
                q = pack v : f16[1,128] @l1
                store A[0,:] <- q
        when c == 1
            for t in 0..1
                h = load B[0,:] : f16[1,128] @l1
                v = unpack h : f32[1,128] @reg
                q = pack v : f16[1,128] @l1
                store out[0,:] <- q
"#,
        BTreeMap::from([
            ("src".into(), 4096),
            ("A".into(), 8192),
            ("B".into(), 8192),
            ("out".into(), 12288),
        ]),
    );
}
