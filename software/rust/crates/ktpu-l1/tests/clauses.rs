//! One test per L1 clause the tier-0 vectors leave out
//! (docs/spec/stack/l1/program.md): each checks the clause's behaviour on a
//! small text, or that its refusal names the clause.

mod common;

use std::collections::BTreeMap;

use common::{core_words, hex256, machine, prog_steps, vectors};
use ktpu_l0::{Word, WordOps};
use ktpu_l1::build::{self, Opts};
use ktpu_l1::lower::image::{self, UNKNOWN};
use ktpu_l1::lower::imem::{rewrite, Resident};
use ktpu_l1::lower::node::NodeProgram;
use ktpu_l1::text::{emit, parse, read, Env, Expander, Term, Val};
use ktpu_l1::Error;
use ktpu_machine::{Machine, UnitKind};
use serde_json::{json, Value};

// ------------------------------------------------------------------ helpers

fn clause<T: std::fmt::Debug>(r: Result<T, Error>) -> String {
    match r {
        Ok(v) => panic!("expected a refusal, got {v:?}"),
        Err(e) => e.clause,
    }
}

fn progs_on(
    m: &Machine,
    text: &str,
    binds: &[(&str, i128)],
    nodes: Option<u32>,
) -> Result<Vec<NodeProgram>, Error> {
    let module = read(text, "<t>")?;
    let b: BTreeMap<String, i128> = binds.iter().map(|(k, v)| (k.to_string(), *v)).collect();
    build::programs(&module, "f", &b, m, nodes, None)
}

fn progs(text: &str, binds: &[(&str, i128)]) -> Result<Vec<NodeProgram>, Error> {
    progs_on(&machine(), text, binds, None)
}

fn kind(m: &Machine) -> impl Fn((u32, u32)) -> String + '_ {
    move |c| {
        for k in [UnitKind::Vc, UnitKind::Mg, UnitKind::Sr] {
            if m.units(k).iter().any(|u| (u.0, u.1) == c) {
                return k.tag().to_string();
            }
        }
        "?".into()
    }
}

/// The node program's steps as the spec runner writes them.
fn steps(body: &str) -> Value {
    let m = machine();
    let text = format!("target ktpu.v9\nfn f.l1(a: f16[4096] @dram)\n{body}");
    let got = progs(&text, &[("a", 4096)]).unwrap();
    let k = kind(&m);
    prog_steps(&k, &got[0].program)
}

fn steps_err(body: &str) -> String {
    let text = format!("target ktpu.v9\nfn f.l1(a: f16[4096] @dram)\n{body}");
    clause(progs(&text, &[("a", 4096)]))
}

fn img(text: &str) -> Result<Vec<String>, Error> {
    let m = machine();
    let module = read(&format!("target ktpu.v9\n{text}"), "<t>")?;
    let (words, _) = image::image(&module, &m, "k", &[], UNKNOWN)?;
    Ok(core_words(&words))
}

fn img_err(text: &str) -> String {
    clause(img(text))
}

/// v9t6 with a second unpack / pack queue (`vc.dualq` 1).
fn dualq_machine() -> Machine {
    let path = ktpu_machine::machines_dir().join("multimesh_v9t6.json");
    let mut doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    doc["machine"]["vc"]["dualq"] = json!(1);
    Machine::from_json(&doc.to_string()).unwrap()
}

/// `img` on a core with a second queue.
fn img_dq(text: &str) -> Result<Vec<String>, Error> {
    let m = dualq_machine();
    let module = read(&format!("target ktpu.v9\n{text}"), "<t>")?;
    let (words, _) = image::image(&module, &m, "k", &[], UNKNOWN)?;
    Ok(core_words(&words))
}

#[test]
fn rul_003_second_queue_refused_without_dualq() {
    for t in [
        "image k()\n    vunpk v0 <- l1[0] f16 q=1\n    halt\n",
        "image k()\n    vpack v0 -> l1[0] f16 q=1\n    halt\n",
        "image k()\n    vsync unpack on=fill q=1\n    halt\n",
        "image k()\n    vwait unpack rx n=1 q=1\n    halt\n",
        "image k()\n    vbar q=1\n    halt\n",
    ] {
        assert_eq!(img_err(t), "RUL.003", "{t}");
        assert!(img_dq(t).is_ok(), "{t}");
    }
    assert!(img("image k()\n    vunpk v0 <- l1[0] f16 q=0\n    halt\n").is_ok());
}

fn one_line(t: &str) -> String {
    emit(&parse(t, "<t>").unwrap())
}

// --------------------------------------------------------------------- text

#[test]
fn l1_txt_011_spans_members_tuples_slices() {
    let s = &parse(
        "x a..b i in 0..4 t += 2 () (x,) (x, y) v[1 : 2, 1 :, : 2, :, *]",
        "<t>",
    )
    .unwrap()[0];
    assert!(
        matches!(&s.args[0], Term::Name(n) if n == "a..b") || matches!(&s.args[0], Term::Span(..))
    );
    assert!(matches!(&s.args[1], Term::Member(..)));
    assert!(matches!(&s.args[2], Term::AddAssign(..)));
    assert_eq!(s.args[3], Term::Tuple(vec![]));
    assert!(matches!(&s.args[4], Term::Tuple(v) if v.len() == 1));
    assert!(matches!(&s.args[5], Term::Tuple(v) if v.len() == 2));
    let Term::View(_, sl) = &s.args[6] else {
        panic!()
    };
    assert!(matches!(&sl[0], Term::Slice(Some(_), Some(_))));
    assert!(matches!(&sl[1], Term::Slice(Some(_), None)));
    assert!(matches!(&sl[2], Term::Slice(None, Some(_))));
    assert!(matches!(&sl[3], Term::Slice(None, None)));
    assert_eq!(sl[4], Term::NewAxis);
}

#[test]
fn l1_txt_012_computed_target() {
    let s = &parse("q{i*4 + u} = mark vc0", "<t>").unwrap()[0];
    assert!(matches!(&s.target, Some(Term::Computed(p, _, None)) if p == "q"));
    assert_eq!(
        one_line("q{i*4 + u} = mark vc0"),
        "q{i * 4 + u} = mark vc0\n"
    );
}

#[test]
fn l1_txt_013_canonical_text() {
    let cases = [
        (
            "x 0x80100000 16384 1048577",
            "x 0x8010_0000 16384 1048577\n",
        ),
        ("x 16K 1M 3G", "x 16K 1M 3G\n"),
        (
            "x inf, -inf, nan, 1e-05, 1e16, 0.0001",
            "x inf, -inf, nan, 1e-05, 1e+16, 0.0001\n",
        ),
        ("x inf -inf", "x inf - inf\n"),
        ("x k=1 a", "x a k=1\n"),
        ("tile bq=32", "tile bq = 32\n"),
        ("x a,b ->c", "x a, b -> c\n"),
    ];
    for (t, want) in cases {
        let got = one_line(t);
        assert_eq!(got, want, "{t}");
        assert_eq!(one_line(&got), got, "{t} is stable");
    }
    let block = "a 1 : n\nbb 2 : m\nf x\n    y\nz\n";
    let got = one_line(block);
    assert_eq!(got, "a 1   : n\nbb 2  : m\n\nf x\n    y\n\nz\n");
    assert_eq!(one_line(&got), got);
}

#[test]
fn l1_txt_014_errors_name_line_and_column() {
    let e = parse("x a\ny $\n", "<f>").unwrap_err();
    assert_eq!(e.clause, "L1.TXT.014");
    assert_eq!((e.line, e.col), (2, 3));
    assert!(e.to_string().contains("<f>:2:3"), "{e}");
}

#[test]
fn l1_txt_015_dedent_to_no_column() {
    let e = parse("a\n    b\n  c\n", "<f>").unwrap_err();
    assert_eq!(e.clause, "L1.TXT.015");
    assert!(e.message.contains("dedent"));
}

// ------------------------------------------------------------------- module

#[test]
fn l1_mod_003_vregs_vcopies() {
    let m = read("target ktpu.v9 vregs=128 vcopies=2\n", "<t>").unwrap();
    assert_eq!((m.vregs, m.vcopies), (128, 2));
    assert_eq!(m.target_line(), "target ktpu.v9 vregs=128 vcopies=2");
    let d = read("target ktpu.v9\n", "<t>").unwrap();
    assert_eq!(
        (d.vregs, d.vcopies, d.target_line()),
        (32, 4, "target ktpu.v9".to_string())
    );
    assert_eq!(
        clause(read("target ktpu.v9 vregs=48\n", "<t>")),
        "L1.MOD.003"
    );
    assert_eq!(
        clause(read("target ktpu.v9 vcopies=5\n", "<t>")),
        "L1.MOD.003"
    );
}

#[test]
fn l1_mod_007_one_target() {
    assert_eq!(clause(read("target a\ntarget b\n", "<t>")), "L1.MOD.007");
    assert_eq!(clause(read("target 3\n", "<t>")), "L1.MOD.007");
    assert_eq!(clause(read("target a q=1\n", "<t>")), "L1.MOD.007");
}

#[test]
fn l1_mod_008_fn_forms() {
    let m = read("fn f.l1(a: f16[4] @dram) -> f16[4]\n    barrier\n", "<t>").unwrap();
    assert!(m.body("f", "l1").unwrap().ret.is_some());
    assert_eq!(
        clause(read(
            "fn f.l1()\n    barrier\nfn f.l1()\n    barrier\n",
            "<t>"
        )),
        "L1.MOD.008"
    );
    assert_eq!(
        clause(read("fn f.l1(a)\n    barrier\n", "<t>")),
        "L1.MOD.008"
    );
    assert_eq!(clause(read("fn f.l1()\n", "<t>")), "L1.MOD.008");
    assert_eq!(
        clause(read("fn f.l3()\n    barrier\n", "<t>")),
        "L1.MOD.004"
    );
}

#[test]
fn l1_mod_009_one_name_table() {
    assert_eq!(
        clause(read("image k()\n    halt\nmacro k()\n    halt\n", "<t>")),
        "L1.MOD.009"
    );
    assert_eq!(clause(read("image k()\n", "<t>")), "L1.MOD.009");
}

#[test]
fn l1_mod_010_unknown_top_level() {
    let e = read("send vc[0]\n", "<t>").unwrap_err();
    assert_eq!(e.clause, "L1.MOD.010");
    assert!(e.message.contains("target, fn, image, macro"));
}

// ---------------------------------------------------------------- expansion

fn expand(body: &str, env: &[(&str, i128)]) -> Result<String, Error> {
    let m = read(&format!("fn f.l1()\n{body}"), "<t>")?;
    let macros = m.macro_table();
    let r = m.reader();
    let env: Env = env
        .iter()
        .map(|(k, v)| (k.to_string(), Val::Int(*v)))
        .collect();
    let out = Expander::new(&macros, &r, true).stmts(&m.body("f", "l1")?.stmts, &env)?;
    Ok(emit(&out))
}

#[test]
fn l1_exp_005_constant_folding() {
    assert_eq!(
        expand(
            "    x 7 / 2, -7 / 2, 7 % -2, a+1+2, b - 3, 1 < 2, 2 == 3\n",
            &[]
        )
        .unwrap(),
        "x 3, -4, -1, a+3, b+-3, 1, 0\n"
    );
    assert_eq!(expand("    x 7 / 2 -7 / 2\n", &[]).unwrap(), "x 0\n");
    assert_eq!(
        expand("    x n * 2 + buf\n", &[("n", 4)]).unwrap(),
        "x buf+8\n"
    );
    let e = expand("    x 1 / 0\n", &[]).unwrap_err();
    assert_eq!(
        (e.clause.as_str(), e.message.as_str()),
        ("L1.EXP.005", "division by zero")
    );
}

#[test]
fn l1_exp_006_computed_names() {
    assert_eq!(
        expand("    x v{1 + 2} v{i}[0 :]\n", &[("i", 5)]).unwrap(),
        "x v3 v5[0 :]\n"
    );
    assert_eq!(
        expand("    x v{j}\n", &[]).unwrap_err().clause,
        "L1.EXP.006"
    );
}

#[test]
fn l1_exp_007_names_and_macro_ops() {
    let text = "macro m(op, r)\n    op r, k=r\nfn f.l1()\n    expand m(vmov, v3)\n    n = set n\n";
    let m = read(text, "<t>").unwrap();
    let macros = m.macro_table();
    let r = m.reader();
    let env: Env = [("n".to_string(), Val::Int(9))].into();
    let out = Expander::new(&macros, &r, true)
        .stmts(&m.body("f", "l1").unwrap().stmts, &env)
        .unwrap();
    assert_eq!(emit(&out), "vmov v3 k=v3\nn = set 9\n");
}

#[test]
fn l1_exp_008_for_needs_constant_bounds() {
    assert_eq!(
        expand("    for i in 0..n\n        x i\n", &[])
            .unwrap_err()
            .clause,
        "L1.EXP.008"
    );
    assert_eq!(
        expand("    for i in 0..3\n        x i\n", &[]).unwrap(),
        "x 0\nx 1\nx 2\n"
    );
}

#[test]
fn l1_exp_009_when_wants_a_constant() {
    assert_eq!(
        expand("    when c\n        x\n", &[]).unwrap_err().clause,
        "L1.EXP.009"
    );
    assert_eq!(
        expand("    when 1 < 2\n        x\n    when 0\n        y\n", &[]).unwrap(),
        "x\n"
    );
}

#[test]
fn l1_exp_010_expand_arity_and_depth() {
    let text = "macro m(a)\n    x a\nfn f.l1()\n    expand m(1, 2)\n";
    let m = read(text, "<t>").unwrap();
    let (macros, r) = (m.macro_table(), m.reader());
    let e = Expander::new(&macros, &r, true)
        .stmts(&m.body("f", "l1").unwrap().stmts, &Env::new())
        .unwrap_err();
    assert_eq!(e.clause, "L1.EXP.010");
    let text = "macro m()\n    expand m()\nfn f.l1()\n    expand m()\n";
    let m = read(text, "<t>").unwrap();
    let (macros, r) = (m.macro_table(), m.reader());
    let e = Expander::new(&macros, &r, true)
        .stmts(&m.body("f", "l1").unwrap().stmts, &Env::new())
        .unwrap_err();
    assert_eq!(e.clause, "L1.EXP.010");
    assert!(e.message.contains("expands itself"));
}

// --------------------------------------------------------------------- body

#[test]
fn l1_body_005_unbound_parameter() {
    let e = progs(
        "fn f.l1(a: f16[4] @dram, b: f16[4] @dram)\n    barrier\n",
        &[("a", 0)],
    )
    .unwrap_err();
    assert_eq!(e.clause, "L1.BODY.005");
    assert!(
        e.message.contains("unbound parameters [\"b\"]"),
        "{}",
        e.message
    );
}

#[test]
fn l1_body_007_node_blocks() {
    let m = machine();
    let text = "fn f.l1(a: f16[4] @stage(node=1))\n    node 0\n        barrier\n    barrier\n";
    assert_eq!(
        clause(progs_on(&m, text, &[("a", 0)], Some(2))),
        "L1.BODY.007"
    );
    let text = "fn f.l1()\n    node 0\n        barrier\n    node 0\n        barrier\n";
    assert_eq!(clause(progs_on(&m, text, &[], Some(2))), "L1.BODY.007");
    let text = "fn f.l1(a: f16[4] @stage(node=1))\n    node 1\n        barrier\n";
    let got = progs_on(&m, text, &[("a", 4096)], Some(2)).unwrap();
    assert_eq!((got[0].node, got[0].mesh), (Some(1), 1));
    assert_eq!(got[0].program.spans.get(&4096), Some(&8));
}

// -------------------------------------------------------------- node values

#[test]
fn l1_node_009_labels() {
    assert_eq!(steps_err("    label x\n    label x\n"), "L0.STEP.052");
    let np = progs("fn f.l1()\n    goto y\n", &[]).unwrap();
    let e = build::build(&np[0], &machine(), Opts::default()).unwrap_err();
    assert!(e.message.contains("no such label"), "{}", e.message);
}

#[test]
fn l1_node_016_fetch() {
    let got = steps("    fetch vc[0] from a n=4\n    fetch vc[1] from 64 + r2 n=2\n");
    assert_eq!(got[0], json!(["fetch", [2, 1], 4096, 4]));
    assert_eq!(
        got[1],
        json!(["fetch", [2, 2], 64, 2, [[0, [[64, 40, 0]], 2, 64]]])
    );
}

#[test]
fn l1_node_021_binds_and_blocks() {
    assert_eq!(steps_err("    t = barrier\n"), "L1.NODE.021");
    assert_eq!(steps_err("    barrier\n        barrier\n"), "L1.NODE.021");
}

#[test]
fn l1_node_022_unknown_statement() {
    assert_eq!(steps_err("    frobnicate\n"), "L1.NODE.022");
}

#[test]
fn l1_node_023_scratch_register() {
    assert_eq!(steps_err("    r15 = set 1\n"), "L1.NODE.023");
    assert_eq!(
        steps("    r14 = set 1\n")[0],
        json!(["alu", 14, "MOV", 0, 1, true])
    );
}

#[test]
fn l1_node_024_runtime_fields() {
    assert_eq!(steps_err("    store a + r1 + 1 <- 1\n"), "L1.BODY.001");
    assert_eq!(steps_err("    r1 = load r2 * 2\n"), "L1.NODE.024");
    assert_eq!(
        steps("    r1 = load r2\n")[0],
        json!(["load", 1, 0, 2, false])
    );
}

#[test]
fn l1_node_025_alu_forms() {
    let got =
        steps("    r1 = set r2\n    r3 = sub r1, r2\n    r4 = shl r1, 3\n    r5 = remu r4, -1\n");
    assert_eq!(got[0], json!(["alu", 1, "MOV", 0, 2, false]));
    assert_eq!(got[1], json!(["alu", 3, "SUB", 1, 2, false]));
    assert_eq!(got[2], json!(["alu", 4, "SHL", 1, 3, true]));
    assert_eq!(got[3], json!(["alu", 5, "REMU", 4, u64::MAX, true]));
    assert_eq!(steps_err("    r1 = add r2\n"), "L1.NODE.025");
    assert_eq!(steps_err("    r1 = add r2, a.b\n"), "L1.NODE.025");
}

#[test]
fn l1_node_026_load_and_ctrl_reads() {
    let got = steps("    r1 = load a + r3\n    r2 = ctrl dbcnt\n    r3 = ctrl 0x40\n");
    assert_eq!(got[0], json!(["load", 1, 4096, 3, false]));
    assert_eq!(got[1], json!(["load", 2, 0x28, null, true]));
    assert_eq!(got[2], json!(["load", 3, 0x40, null, true]));
}

#[test]
fn l1_node_027_ctrl_from_register() {
    assert_eq!(
        steps("    ctrl il_ring <- r4\n")[0],
        json!(["ctrl", [[0xD0, 4]], true])
    );
}

#[test]
fn l1_node_028_goto_forms() {
    let got = steps(
        "    label l\n    goto l\n    goto l if r1 == r2\n    goto l if r1 > r2\n    goto l if r1 <= 5\n    goto l if r1 > 5 signed\n",
    );
    assert_eq!(got[1], json!(["jump", "l", "ALWAYS", 0, 0, true]));
    assert_eq!(got[2], json!(["jump", "l", "EQ", 1, 2, false]));
    assert_eq!(got[3], json!(["jump", "l", "LTU", 2, 1, false]));
    assert_eq!(got[4], json!(["jump", "l", "LTU", 1, 6, true]));
    assert_eq!(got[5], json!(["jump", "l", "GE", 1, 6, true]));
    assert_eq!(steps_err("    goto l if r1\n"), "L1.NODE.028");
}

#[test]
fn l1_node_029_ring_defaults() {
    assert_eq!(
        steps("    ring mesh=2\n")[0],
        json!(["ring", 2, 0, 1, true])
    );
    assert_eq!(
        steps("    ring mesh=2 slot=3 plain\n")[0],
        json!(["ring", 2, 3, 1, false])
    );
    assert_eq!(steps_err("    ring mesh=2 loud\n"), "L1.NODE.029");
    assert_eq!(steps_err("    ring mesh=2 x=1\n"), "L1.NODE.029");
}

#[test]
fn l1_node_030_wait_slot_and_bell() {
    assert_eq!(
        steps("    wait slot 2 n=3\n")[0],
        json!(["wait_slot", 2, 3, false])
    );
    assert_eq!(steps("    wait bell 1\n")[0], json!(["wait_bell", 1, 1]));
}

#[test]
fn l1_node_031_signal_values() {
    assert_eq!(steps("    signal 7\n")[0], json!(["signal", 7, 0]));
    assert_eq!(steps_err("    signal 1 2 3\n"), "L1.NODE.031");
}

#[test]
fn l1_node_032_push_forms() {
    let got = steps("    push vc[0] buf=1 at=2 <- a n=4 signal ack=mg[1]\n");
    let want = [(1, 248), (2, 232), (3, 224), (1, 216), (2, 212), (1, 208)]
        .into_iter()
        .fold(Word::ZERO, |w, (v, b)| w | <Word as WordOps>::placed(v, b));
    assert_eq!(
        got[0],
        json!(["push", [2, 1], hex256(&want), 4096, 4, false])
    );
    assert_eq!(steps_err("    push vc[0] <- a n=257\n"), "L1.NODE.032");
}

#[test]
fn l1_node_033_recv_from_any() {
    assert_eq!(
        steps("    recv n=2 -> a\n")[0],
        json!(["recv", 4096, 2, null])
    );
}

#[test]
fn l1_node_034_waits_and_marks() {
    assert_eq!(steps("    wait vc[0]\n")[0], json!(["wait", [2, 1], null]));
    assert_eq!(steps_err("    mark vc[0]\n"), "L1.NODE.034");
    assert_eq!(steps_err("    wait vc[0] upto=q\n"), "L1.NODE.034");
    assert_eq!(
        steps_err("    t = mark vc[0]\n    wait vc[1] upto=t\n"),
        "L0.STEP.021"
    );
}

#[test]
fn l1_node_035_move_forms() {
    assert_eq!(
        steps_err("    move posted\n        fill w(a, (1, 32))\n"),
        "L1.NODE.035"
    );
    assert_eq!(
        steps_err("    t = move\n        fill w(a, (1, 32))\n"),
        "L1.NODE.035"
    );
    assert_eq!(steps_err("    move\n"), "L1.NODE.035");
}

#[test]
fn l1_node_036_wait_moves_needs_a_post() {
    assert_eq!(
        steps_err("    t = mark vc[0]\n    wait moves t\n"),
        "L1.NODE.036"
    );
}

#[test]
fn l1_unit_003_row_units_and_counts() {
    assert_eq!(
        steps_err("    send sr[0]\n        job nw=8\n"),
        "L1.UNIT.003"
    );
    assert_eq!(steps_err("    wait vc[2]\n"), "L1.UNIT.003");
}

#[test]
fn l1_unit_004_own_mesh_only() {
    let m = machine();
    let text = "fn f.l1()\n    wait mesh1.vc[0]\n";
    assert_eq!(clause(progs_on(&m, text, &[], Some(2))), "L1.UNIT.004");
}

// ------------------------------------------------------------------ clusters

fn mg(body: &str) -> Value {
    steps(&format!("    send mg[0]\n{body}"))[0][2].clone()
}

#[test]
fn l1_mg_003_fill_count_refused() {
    assert_eq!(
        steps_err("    send mg[0]\n        fill A0 <- a n=256\n"),
        "ISA.MG.018"
    );
}

#[test]
fn l1_mg_007_gemm_nk() {
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 4x4x3 a=A0 b=B0 acc=0\n"),
        "ISA.MG.099"
    );
    assert_eq!(
        mg("        gemm 4x4x4 a=A0 b=B0 acc=0\n")[0],
        json!("GEMM gm=4 gn=4 nk=4 anchor=40")
    );
}

#[test]
fn l1_mg_008_emit_to_a_unit() {
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 1x1x1 emit\n"),
        "L1.MG.008"
    );
    let got = mg("        gemm 1x1x1 emit to=vc[0] l1=4 count\n        drain n=1 to=vc[0] l1=4 fused count\n");
    assert_eq!(
        got[0],
        json!("GEMM addr=0x80 gm=1 gn=1 nk=1 anchor=40 emit=1 dnode=1 dst_x=2 dst_y=1 dflags=2")
    );
    assert_eq!(
        got[1],
        json!("DRAIN addr=0x80 n=1 nk=1 anchor=40 fuse=1 dnode=1 dst_x=2 dst_y=1 dflags=2")
    );
}

#[test]
fn l1_mg_012_post() {
    assert_eq!(
        mg("        post n=4 to=vc[1]\n")[0],
        json!("POST n=4 nk=1 anchor=40 dst_x=2 dst_y=2")
    );
    let m = machine();
    let text = "fn f.l1()\n    send mg[0]\n        post n=4 to=mesh1.vc[1]\n";
    assert_eq!(clause(progs_on(&m, text, &[], Some(2))), "L1.MG.012");
}

#[test]
fn l1_mg_014_cluster_statements() {
    assert_eq!(steps_err("    send mg[0]\n        run k()\n"), "L1.MG.014");
}

#[test]
fn l1_mg_015_fill_entry_register_count_banks() {
    let got = steps("    send mg[0]\n        fill B1 <- a n=r3 at=5\n");
    assert_eq!(
        got[0][2][0],
        json!("FILL addr=0x1000 sel=1 nk=1 anchor=40 eoff=5 fbank=1")
    );
    assert_eq!(got[0][3][0][2], json!(3));
    assert_eq!(
        steps_err("    send mg[0]\n        fill C0 <- a n=1\n"),
        "L1.MG.015"
    );
}

#[test]
fn l1_mg_017_fill_recv_forms() {
    assert_eq!(
        mg("        fill B0 recv n=8\n")[0],
        json!("FILL n=8 sel=1 nk=1 anchor=40 recv=1")
    );
    assert_eq!(
        steps_err("    send mg[0]\n        fill A0 recv n=8 at=1\n"),
        "L1.MG.002"
    );
}

#[test]
fn l1_mg_018_gemm_operands() {
    assert_eq!(
        mg("        gemm 2x2x1 aoff=1 boff=2 anchor=7 acc\n")[0],
        json!("GEMM acc=1 gm=2 gn=2 nk=1 anchor=7 aoff=1 boff=2")
    );
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 2x2x1 a=B0\n"),
        "L1.MG.018"
    );
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 2x2x1 fast\n"),
        "L1.MG.018"
    );
}

#[test]
fn l1_mg_019_memory_drains() {
    assert_eq!(
        mg("        drain a n=2\n")[0],
        json!("DRAIN addr=0x1000 n=2 nk=1 anchor=40")
    );
    let m = machine();
    let text = "fn f.l1(a: f16[4096] @dram)\n    send mg[0]\n        drain a n=2 mesh=1\n";
    let got = progs_on(&m, text, &[("a", 4096)], Some(2)).unwrap();
    let s = prog_steps(&kind(&m), &got[0].program);
    assert_eq!(
        s[0][2][0],
        json!("DRAIN addr=0x1000 n=2 nk=1 anchor=40 dmesh=1 dfin=16")
    );
}

#[test]
fn l1_mg_020_unit_drains() {
    assert_eq!(
        mg("        drain n=2 to=mg[1] l1=3 buf=1 signal credit ack=vc[0]\n")[0],
        json!("DRAIN addr=0x60 n=2 nk=1 anchor=40 dnode=1 dst_x=1 dst_y=2 dbuf=1 dflags=1 dack_y=1 dack_x=2 credit=1")
    );
    assert_eq!(
        steps_err("    send mg[0]\n        drain a n=2 count\n"),
        "L1.MG.020"
    );
    assert_eq!(
        steps_err("    send mg[0]\n        drain a n=2 quick\n"),
        "L1.MG.020"
    );
}

#[test]
fn l1_mg_021_fused_drains_collect() {
    assert_eq!(
        steps_err("    send mg[0]\n        drain a n=1 fused\n"),
        "L1.MG.021"
    );
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 1x1x1 emit=a\n        drain a n=2 fused\n"),
        "L1.MG.021"
    );
    assert_eq!(
        steps_err("    send mg[0]\n        gemm 1x1x1 emit=a\n"),
        "L1.MG.021"
    );
}

// -------------------------------------------------------------- vector sends

fn vc(text: &str, body: &str) -> Value {
    let m = machine();
    let full = format!("target ktpu.v9\n{text}fn f.l1(a: f16[4096] @dram)\n{body}");
    let got = progs(&full, &[("a", 4096)]).unwrap();
    let k = kind(&m);
    prog_steps(&k, &got[0].program)
}

#[test]
fn l1_vc_004_patched_descriptor_base() {
    let got = vc(
        "image k()\n    halt\n",
        "    send vc[0]\n        desc a1 = 64 + r2 walk=(32, 1)\n        run k()\n",
    );
    let s = &got[0];
    assert_eq!(s[2][0], json!(["DESC", 1, 0, "0x40"]));
    assert_eq!(s[3][0], json!([0, [[212, 34, 0], [63, 6, 34]], 2, 64]));
}

#[test]
fn l1_vc_006_core_send_statements() {
    assert_eq!(
        steps_err("    send vc[0]\n        fill A0 <- a n=1\n"),
        "L1.VC.006"
    );
}

#[test]
fn l1_vc_007_label_forgets_the_core() {
    let got = vc(
        "image k()\n    halt\n",
        "    send vc[0]\n        run k()\n    label x\n    send vc[0]\n        run k()\n",
    );
    let imem = |s: &Value| {
        s[2].as_array()
            .unwrap()
            .iter()
            .filter(|w| w[0] == "IMEM")
            .count()
    };
    assert_eq!((imem(&got[0]), imem(&got[2])), (1, 1));
    let warm = vc(
        "image k()\n    halt\n",
        "    send vc[0]\n        run k()\n    send vc[0]\n        run k()\n",
    );
    assert_eq!(imem(&warm[1]), 0);
}

#[test]
fn l1_vc_008_descriptor_refusals() {
    let five =
        "    send vc[0]\n        desc a0 = 0 walk=((1, 1), (1, 1), (1, 1), (1, 1), (1, 1))\n";
    assert_eq!(steps_err(five), "L1.VC.008");
    assert_eq!(
        steps_err("    send vc[0]\n        desc a0 = 0 walk=(n, 1)\n"),
        "L1.VC.008"
    );
    assert_eq!(
        steps_err("    send vc[0]\n        desc a0 = q walk=(1, 1)\n"),
        "L1.VC.008"
    );
}

#[test]
fn l1_vc_009_peer_ack_and_mesh() {
    let got = vc(
        "",
        "    send vc[0]\n        desc a0 = peer(3, ack=(1, 2), mesh=2) walk=(32, 1)\n",
    );
    let v = 3u64 | (2 << 4 | 1) << 16 | 2 << 24;
    assert_eq!(got[0][2][0], json!(["DESC", 0, 0, format!("{v:#x}")]));
    let m = machine();
    let text = "fn f.l1()\n    send vc[0]\n        desc a0 = peer(3, mesh=0, fin=mesh1.vc[0]) walk=(32, 1)\n";
    assert_eq!(clause(progs_on(&m, text, &[], Some(2))), "L1.VC.009");
}

// -------------------------------------------------------------------- images

#[test]
fn l1_img_007_more_math() {
    let got = img("image k()\n    vmin v1, v2, v3\n    vneg v1, v2\n    vabs v4, v5\n    halt\n")
        .unwrap();
    assert!(got[1].starts_with("vmin vd=1 va=2 vb=3"), "{got:?}");
    assert_eq!(got[2], "vneg vd=1 va=2");
    assert_eq!(got[3], "vabs vd=4 va=5");
}

#[test]
fn l1_img_019_marks() {
    let got =
        img("image k()\n    vmark m3 on=pack slack=2\n    vsync fe mark=m3\n    halt\n").unwrap();
    assert_eq!(got[0], "VMARK waiter=5 on=3 slack=2 mark=3");
    assert_eq!(got[1], "VSYNC marked=1 mark=3");
}

#[test]
fn l1_img_025_setvl_forgets_vl() {
    let got =
        img("image k()\n    vmov v1, v2 vl=16\n    setvl s1\n    vmov v1, v2 vl=16\n    halt\n")
            .unwrap();
    assert_eq!(
        got.iter().filter(|w| w.starts_with("CHKVL")).count(),
        2,
        "{got:?}"
    );
}

#[test]
fn l1_img_029_written_configuration() {
    let got = img("image k()\n    chkvl vl=128\n    vmov v1, v2\n    xb mode=xor k=1\n    ustr 2 8\n    vunpk v1 <- l1[0] f16 walk=(2, 8)\n    word 0x12345\n    vmov v1, v2\n    halt\n").unwrap();
    assert_eq!(
        got[0],
        "CHKVL vl1=127 d_stride=1 c_stride=1 b_stride=1 a_stride=1"
    );
    assert_eq!(got[1], "vmov vd=1 va=2");
    assert_eq!(got[2], "XB xk=1 xm=1");
    assert_eq!(got[3], "USTR s1=8 s0=2");
    assert!(got[4].starts_with("VUNPK"));
    assert!(
        got[6].starts_with("CHKVL"),
        "vcfg/word leave the state unknown: {got:?}"
    );
}

#[test]
fn l1_img_032_run_arity_and_names() {
    assert_eq!(
        steps_err("    send vc[0]\n        run nope()\n"),
        "L1.IMG.032"
    );
    let e = clause(progs(
        "target ktpu.v9\nimage k(x)\n    halt\nfn f.l1()\n    send vc[0]\n        run k()\n",
        &[],
    ));
    assert_eq!(e, "L1.IMG.032");
}

#[test]
fn l1_img_033_descriptors_lead() {
    assert_eq!(
        img_err("image k()\n    halt\n    desc a0 = 0 walk=(1, 1)\n"),
        "L1.IMG.033"
    );
    let ok = vc(
        "image k(x)\n    when x\n        desc a1 = x walk=(32, 1)\n    halt\n",
        "    send vc[0]\n        run k(64)\n",
    );
    assert_eq!(ok[0][2][0], json!(["DESC", 1, 0, "0x40"]));
}

#[test]
fn l1_img_034_unknown_ops_and_arity() {
    assert_eq!(img_err("image k()\n    vfoo v1\n"), "L1.IMG.034");
    assert_eq!(img_err("image k()\n    vmul v1, v2\n"), "L1.IMG.034");
}

#[test]
fn l1_img_035_chunk_select_on_scalar() {
    assert_eq!(img_err("image k()\n    vmov v1, s1[0 :]\n"), "L1.IMG.035");
}

#[test]
fn l1_img_037_crossbar_operands() {
    assert_eq!(
        img_err("image k()\n    vmul v1, xor(v2, 1), v3\n"),
        "L1.IMG.037"
    );
    assert_eq!(
        img_err("image k()\n    vadd v1, v2, xor(v3, 1)\n    vfma v1, v2, v3, xor(v4, 1)\n"),
        "L1.IMG.037"
    );
}

#[test]
fn l1_img_038_vl_range_and_keep() {
    assert_eq!(img_err("image k()\n    vmov v1, v2 vl=129\n"), "L1.IMG.038");
    let got = img("image k()\n    vmov v1, v2 vl=keep\n    halt\n").unwrap();
    assert_eq!(got[0], "vmov vd=1 va=2", "keep emits no VL word: {got:?}");
}

#[test]
fn l1_img_039_unless_and_compare_predicates() {
    let got = img("image k()\n    vmov v1, v2 unless=p2\n    halt\n").unwrap();
    assert!(got.iter().any(|w| w == "XB pr=2 pm=2"), "{got:?}");
    assert_eq!(
        img_err("image k()\n    vmov v1, v2 if=p1 unless=p2\n"),
        "L1.IMG.039"
    );
    assert_eq!(
        img_err("image k()\n    vcmplt p1, v2, v3 if=p2\n"),
        "L1.IMG.039"
    );
}

#[test]
fn l1_img_040_walk_forms() {
    let got = img_dq("image k()\n    vunpk v1[2 :] <- l1[3] f32 rel q=1\n    halt\n").unwrap();
    assert!(
        got.iter()
            .any(|w| w == "VUNPK reg=1 mode=2 n1=5 cb=2 rel=1 q=1 off=3"),
        "{got:?}"
    );
    assert_eq!(
        img_err("image k()\n    vunpk v1[2] <- l1[0] f16\n"),
        "L1.IMG.040"
    );
    assert_eq!(
        img_err("image k()\n    vunpk v1 <- l1[0] f16 fast\n"),
        "L1.IMG.040"
    );
}

#[test]
fn l1_img_041_mx7_packs() {
    let got = img("image k()\n    vpack v1 -> l1[0] mx7b\n    halt\n").unwrap();
    assert!(
        got.iter().any(|w| w == "VPACK reg=1 mode=3 n1=7 cb=1"),
        "{got:?}"
    );
    assert_eq!(
        img_err("image k()\n    vpack v1[0 :] -> l1[0] mx7a\n"),
        "L1.IMG.041"
    );
    assert_eq!(
        img_err("image k()\n    vunpk v1 <- l1[0] mx7a\n"),
        "L1.IMG.041"
    );
}

#[test]
fn l1_img_043_peer_drains() {
    let got = img("image k()\n    vdrain a1 <- l1[2] to=(1, 2) buf=3 signal\n    halt\n").unwrap();
    assert_eq!(
        got[0],
        "VDRAIN signal=1 node=1 ad=1 dst_x=1 dst_y=2 buf_id=3 off=2"
    );
    assert_eq!(
        img_err("image k()\n    vdrain a1 <- l1[2] buf=3\n"),
        "L1.IMG.043"
    );
}

#[test]
fn l1_img_044_vsync_forms() {
    let got = img_dq("image k()\n    vsync math on=unpack1 q=1\n    halt\n").unwrap();
    assert_eq!(got[0], "VSYNC waiter=2 on=5 q=1");
    assert_eq!(
        img_err("image k()\n    vsync fe on=fill mark=m1\n"),
        "L1.IMG.044"
    );
    assert_eq!(
        img_err("image k()\n    vsync fe mark=m1 slack=2\n"),
        "L1.IMG.044"
    );
}

#[test]
fn l1_img_045_credits() {
    let got =
        img_dq("image k()\n    vwait drain cr n=4 q=1\n    vpost (1, 2) n=8\n    halt\n").unwrap();
    assert_eq!(got[0], "VWAIT waiter=6 on=7 n=4 q=1 counter=1");
    assert_eq!(got[1], "VPOST dst_x=1 dst_y=2 n=8");
}

#[test]
fn l1_img_046_vbar_forms() {
    assert_eq!(
        img_dq("image k()\n    vbar q=1\n    halt\n").unwrap()[0],
        "VBAR q=1"
    );
    assert_eq!(img_err("image k()\n    vbar 1\n"), "L1.IMG.046");
}

#[test]
fn l1_img_047_ainc_range() {
    assert_eq!(img_err("image k()\n    ainc a0 += 131072\n"), "L1.IMG.047");
    assert_eq!(
        img("image k()\n    ainc a0 += -131072\n    halt\n").unwrap()[0],
        "AINC inc=-131072"
    );
}

#[test]
fn l1_img_048_pack_pointers() {
    let got = img("image k()\n    pptr 5\n    pinc 6\n    halt\n").unwrap();
    assert_eq!(
        (got[0].as_str(), got[1].as_str()),
        ("PPTR ptr=5", "PINC ptr=6")
    );
}

#[test]
fn l1_img_049_seti_forms() {
    let got = img("image k()\n    seti k3 = 2\n    halt\n").unwrap();
    assert_eq!(
        (got[0].as_str(), got[1].as_str()),
        ("VSETI to_k=1", "imm=0x2")
    );
    assert_eq!(img_err("image k()\n    seti s1 = q\n"), "L1.IMG.049");
}

#[test]
fn l1_img_050_loop_refusals() {
    assert_eq!(
        img_err("image k()\n    loop s1\n        loop s2\n            vmov v1, v2\n"),
        "L1.IMG.050"
    );
    assert_eq!(img_err("image k()\n    loop s1\n"), "L1.IMG.050");
}

#[test]
fn l1_img_051_vcmpeq() {
    let got = img("image k()\n    vcmpeq p1, v2, v3\n    halt\n").unwrap();
    assert!(
        got.iter().any(|w| w.starts_with("vcmpeq va=2 vb=3")),
        "{got:?}"
    );
}

// ------------------------------------------------------------------- mover

fn mv(body: &str) -> Value {
    steps(&format!("    move\n{body}"))[0].clone()
}

#[test]
fn l1_mov_009_plain_address_forms() {
    let got = mv("        copy a -> 8192 bytes=64\n");
    assert_eq!(got[1][0], json!("HDR base=0x1000 ndim=1"));
    assert_eq!(
        steps_err("    move\n        copy a -> 8192 bytes=64 ew=16\n"),
        "L1.MOV.009"
    );
}

#[test]
fn l1_mov_011_levels_axes_windows() {
    let got = mv("        copy w(a, (2, 32, 1, 1), (4, 0)) -> w(8192, (8, 32), win2=(0, 4))\n");
    let w = got[1].as_array().unwrap();
    assert!(w.contains(&json!("AXIS axis=1 astep=1")), "{w:?}");
    assert!(w.contains(&json!("DIM level=1 count=4")), "{w:?}");
    assert!(w.contains(&json!("WIN sel=1 axis=1 extent=4")), "{w:?}");
    assert_eq!(
        steps_err("    move\n        copy w(a) -> w(a, (1, 32))\n"),
        "L1.MOV.011"
    );
}

#[test]
fn l1_mov_012_xform_and_unknown_ops() {
    let got = mv("        xform w(a, (8, 32)) -> w(8192, (1, 128)) id=3 mode=2\n");
    assert_eq!(got[1][0], json!("HDR base=0x1000 ndim=1 xid=3 xmode=2"));
    assert_eq!(
        steps_err("    move\n        shuffle w(a, (1, 32))\n"),
        "L1.MOV.012"
    );
}

#[test]
fn l1_mov_013_element_width() {
    assert_eq!(
        steps_err("    move\n        copy w(a, (1, 32)) -> w(a, (1, 32)) ew=4\n"),
        "L1.MOV.013"
    );
}

#[test]
fn l1_mov_014_burst_and_coalescing() {
    let got = mv("        copy w(a, (1, 32)) -> w(8192, (1, 32)) burst=3 nocoal\n");
    let ctrl = got[1].as_array().unwrap().last().unwrap().clone();
    assert_eq!(ctrl, json!("CTRL ew=2 flags=0x60 go=1"));
}

#[test]
fn l1_mov_016_register_fields() {
    let got = mv("        fill w(a, (r2, 32)) value=r3\n");
    let p = got[2].as_array().unwrap();
    assert_eq!(p.len(), 2);
    assert_eq!(p[1][1], json!([[0, 32, 0]]));
}

#[test]
fn l1_mov_017_scatter_fp_and_missing_keywords() {
    let got = mv("        scatter w(a, (2, 32)) -> 8192 index=a n=2 pitch=32 words=1 fp\n");
    let ctrl = got[1].as_array().unwrap().last().unwrap().clone();
    assert_eq!(ctrl, json!("CTRL mode=6 ew=2 flags=0x8 go=1 fp=1"));
    assert_eq!(
        steps_err("    move\n        gather a -> w(a, (1, 32)) n=1 pitch=1 words=1\n"),
        "L1.MOV.017"
    );
}

// ------------------------------------------------------------------ row units

#[test]
fn l1_sr_001_row_unit_sends() {
    let m = Machine::named("sru_v9t7").unwrap();
    let text = "fn f.l1(x: f16[4096] @dram, y: f16[4096] @dram)\n    send sr[0]\n        job nw=8 nsr=4 out=f16\n        pass 0\n            fin max R0 <- R0, ST1\n        run a=x out=y\n        run a=x out=y\n";
    let got = progs_on(&m, text, &[("x", 4096), ("y", 65536)], None).unwrap();
    let send = match &got[0].program.steps[0] {
        ktpu_l0::Stmt::Send { words, .. } => words.clone(),
        other => panic!("{other:?}"),
    };
    let cfg = |w: &Word| w.get(252, 4) == 1;
    let n_cfg = send.iter().filter(|w| cfg(w)).count();
    assert!(n_cfg > 0, "the first run writes the configuration");
    assert_eq!(
        send.len(),
        n_cfg + 2,
        "the second run writes no configuration word again"
    );
    let e = progs_on(
        &m,
        "fn f.l1()\n    send sr[0]\n        run a=0 out=0\n",
        &[],
        None,
    )
    .unwrap_err();
    assert_eq!(e.clause, "L1.SR.001");
}

// ------------------------------------------------------------------ lowering

#[test]
fn l1_low_009_wait_moves_past_a_label() {
    let np = progs("fn f.l1(a: f16[64] @dram)\n    t = move posted\n        fill w(a, (1, 32))\n    label x\n    wait moves t\n", &[("a", 4096)]).unwrap();
    let e = build::build(&np[0], &machine(), Opts::default()).unwrap_err();
    assert_eq!(e.clause, "L0.STEP.064");
}

#[test]
fn l1_low_014_imem_without_run() {
    let m = machine();
    let isa = ktpu_isa::unit::vector::VecIsa::new(&m).unwrap();
    let w = ktpu_isa::unit::vector::VecCu::Imem {
        addr: 0,
        word: 0xF800_0000,
    }
    .encode(&isa)
    .unwrap();
    let mut r = Resident::new(m.vc.imem_words, m.vc.run_reach);
    let e = rewrite(&isa, &[w], &mut r).unwrap_err();
    assert_eq!(e.clause, "L1.LOW.014");
}

#[test]
fn l1_low_015_node_steps_flush_units() {
    let np = progs(
        "target ktpu.v9\nimage k()\n    halt\nfn f.l1()\n    send vc[0]\n        run k()\n    ring mesh=1\n    send vc[0]\n        run k()\n",
        &[],
    )
    .unwrap();
    let b = build::build(&np[0], &machine(), Opts::default()).unwrap();
    let pkg = b.build(true, false);
    let ops: Vec<&str> = pkg.steps.iter().map(|s| s.op.name()).collect();
    assert_eq!(ops[..3], ["DISPATCH", "RING", "DISPATCH"]);
}

#[test]
fn l1_vectors_present() {
    assert!(!vectors("l1", "l1.steps").is_empty());
}
