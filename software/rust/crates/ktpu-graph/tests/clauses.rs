//! One test a clause of `docs/spec/stack/dsl/graph.md` this crate holds:
//! the text spellings and the refusals of a malformed graph.

mod index_read;

use ktpu_graph::text;
use ktpu_graph::*;
use ktpu_machine::Machine;
use serde_json::{json, Value};

fn graph(body: Value) -> Value {
    json!({
        "format": "ktpu.graph", "version": 1, "name": "k", "target": "ktpu.v9",
        "machine": {"mg": 2, "vc": 2, "nodes": 1}, "consts": {},
        "ports": [
            {"name": "x", "role": "in", "spec": {"dtype": "f16", "shape": [64, 128], "layout": [], "space": "dram"}},
            {"name": "y", "role": "out", "spec": {"dtype": "f16", "shape": [64, 128], "layout": [], "space": "dram"}}
        ],
        "scratch": [],
        "body": body,
    })
}

fn var(n: &str, c: i64) -> Value {
    json!({"const": 0, "terms": [[{"var": n}, c]]})
}

fn vc_par(body: Value) -> Value {
    json!({"kind": "par", "var": "c1", "lo": 0, "hi": 2,
           "unit": {"kind": "vc", "index": var("c1", 1)}, "body": body, "where": "k.py:1"})
}

fn load(target: &str) -> Value {
    json!({"kind": "value", "op": "load", "target": target,
           "type": {"dtype": "f16", "shape": [4, 128], "space": "l1", "mode": null},
           "args": [{"view": {"buffer": "x", "axes": [{"run": [0, 4]}, "all"]}}], "kw": [], "where": "k.py:2"})
}

fn l2(g: Value) -> Result<L2Module, ktpu_machine::Refusal> {
    lower(&parse(&g.to_string())?)
}

fn refused(g: Value) -> String {
    l2(g).expect_err("refused").clause
}

#[test]
fn dsl_text_004_number_repr() {
    for (v, want) in [
        (5.0, "5.0"),
        (1e-05, "1e-05"),
        (0.0001, "0.0001"),
        (1e16, "1e+16"),
        (123456789012345678.0, "1.2345678901234568e+17"),
        (1.0 / 3.0, "0.3333333333333333"),
        (-std::f64::consts::LOG2_E, "-1.4426950408889634"),
        (-0.0, "-0.0"),
        (f64::INFINITY, "inf"),
    ] {
        assert_eq!(text::py_float(v), want);
    }
}

#[test]
fn dsl_idx_006_positive_terms_first() {
    let s: Index =
        serde_json::from_value(json!({"const": 4, "terms": [[{"var": "t"}, -1]]})).unwrap();
    assert_eq!(text::index(&s), "4 - t");
    let s: Index = serde_json::from_value(
        json!({"const": 0, "terms": [[{"var": "c"}, 256], [{"var": "t"}, 16]]}),
    )
    .unwrap();
    assert_eq!(text::index(&s), "c*256 + t*16");
}

#[test]
fn dsl_idx_009_atoms_parenthesised() {
    let s: Index = serde_json::from_value(json!({"const": 0, "terms": [
        [{"floor": [{"const": 1, "terms": [[{"var": "k"}, 1]]}, 2]}, 3],
        [{"var": "t"}, -2]
    ]}))
    .unwrap();
    assert_eq!(text::index(&s), "((k + 1) / 2)*3 - t*2");
}

#[test]
fn dsl_spec_007_spec_text() {
    let s: Spec = serde_json::from_value(json!({"dtype": "mx7a", "shape": [6400, 416],
        "layout": [["gm", {"int": 64}], ["nk", {"int": 1}]], "space": "dram"}))
    .unwrap();
    assert_eq!(text::spec(&s), "mx7a[6400, 416] @dram(gm=64, nk=1)");
    let s: Spec = serde_json::from_value(json!({"dtype": "f16", "shape": [6400, 32],
        "layout": [["tile", {"ints": [64, 8]}]], "space": "dram"}))
    .unwrap();
    assert_eq!(text::spec(&s), "f16[6400, 32] @dram(tile=64x8)");
}

#[test]
fn dsl_view_006_view_text() {
    let v: View = serde_json::from_value(json!({"buffer": "x", "axes": ["all", "all"]})).unwrap();
    assert_eq!(text::view(&v), "x");
    let v: View = serde_json::from_value(
        json!({"buffer": "x", "axes": [{"run": [var("t", 4), 4]}, "all", "all"]}),
    )
    .unwrap();
    assert_eq!(text::view(&v), "x[t*4 : +4]");
    let v: View =
        serde_json::from_value(json!({"buffer": "x", "axes": ["new", {"idx": 3}, "all"]})).unwrap();
    assert_eq!(text::view(&v), "x[*, 3]");
}

#[test]
fn dsl_val_005_value_view_columns() {
    let v: ValueView =
        serde_json::from_value(json!({"value": "v1", "axes": ["all", {"run": [32, 64]}]})).unwrap();
    assert_eq!(text::value_view(&v).unwrap(), "v1[:, 32:96]");
    let v: ValueView =
        serde_json::from_value(json!({"value": "a1", "axes": [{"run": [0, 32]}, "all"]})).unwrap();
    assert_eq!(text::value_view(&v).unwrap(), "a1[0 : +32, :]");
}

#[test]
fn dsl_text_003_annotation_columns() {
    let long = "x".repeat(50);
    let lines = vec![
        Line {
            target: Some("a".into()),
            op: "zero".into(),
            args: vec![],
            kw: vec![],
            annot: Some("T".into()),
            body: None,
        },
        Line {
            target: Some(long.clone()),
            op: "zero".into(),
            args: vec![],
            kw: vec![],
            annot: Some("T".into()),
            body: None,
        },
    ];
    let mut out = Vec::new();
    text::block(&lines, 1, &mut out);
    assert_eq!(out[0], format!("    a = zero{}: T", " ".repeat(48 - 12)));
    assert_eq!(out[1], format!("    {long} = zero  : T"));
}

#[test]
fn dsl_graph_001_format_and_version() {
    let mut g = graph(json!([]));
    g["version"] = json!(2);
    assert_eq!(refused(g), "DSL.GRAPH.001");
    assert_eq!(
        parse("{\"format\": \"ktpu.graph\"}").unwrap_err().clause,
        "DSL.GRAPH.001"
    );
}

#[test]
fn dsl_graph_002_tree_prints_as_blocks() {
    let m = l2(graph(json!([vc_par(json!([load("xl1")]))]))).unwrap();
    assert_eq!(
        m.text(),
        "target ktpu.v9\n\nfn k.l2(x: f16[64, 128] @dram, y: f16[64, 128] @dram)\n    \
         par c1 in 0..2 on vc[c1]\n        xl1 = load x[0 : +4]  : f16[4, 128] @l1\n"
    );
}

#[test]
fn dsl_graph_006_operands_defined_before_use() {
    let store = json!({"kind": "store", "dst": {"buffer": "y", "axes": []}, "src": "p9", "where": "k.py:3"});
    assert_eq!(
        refused(graph(json!([vc_par(json!([store]))]))),
        "DSL.GRAPH.006"
    );
    let mut bad = load("xl1");
    bad["args"][0]["view"]["buffer"] = json!("z");
    assert_eq!(
        refused(graph(json!([vc_par(json!([bad]))]))),
        "DSL.GRAPH.006"
    );
}

#[test]
fn dsl_graph_007_variables_bound() {
    let mut l = load("xl1");
    l["args"][0]["view"]["axes"][0] = json!({"run": [var("t9", 4), 4]});
    assert_eq!(refused(graph(json!([vc_par(json!([l]))]))), "DSL.GRAPH.007");
}

#[test]
fn dsl_scope_003_statement_out_of_scope() {
    assert_eq!(refused(graph(json!([load("xl1")]))), "DSL.SCOPE.003");
    let copy = json!({"kind": "mover", "op": "copy", "dst": {"buffer": "y", "axes": []},
        "src": {"buffer": "x", "axes": []}, "flags": [], "kw": [], "where": "k.py:4"});
    assert_eq!(
        refused(graph(json!([vc_par(json!([copy]))]))),
        "DSL.SCOPE.003"
    );
}

#[test]
fn dsl_ctrl_009_empty_block() {
    assert_eq!(refused(graph(json!([vc_par(json!([]))]))), "DSL.CTRL.009");
}

#[test]
fn dsl_ctrl_012_par_inside_a_unit() {
    let mut inner = vc_par(json!([load("xl1")]));
    inner["var"] = json!("c2");
    assert_eq!(
        refused(graph(json!([vc_par(json!([inner]))]))),
        "DSL.CTRL.012"
    );
}

#[test]
fn dsl_ctrl_013_variable_bound_twice() {
    let inner = json!({"kind": "for", "var": "c1", "lo": 0, "hi": 2, "pipe": null,
        "body": [load("xl1")], "where": "k.py:5"});
    assert_eq!(
        refused(graph(json!([vc_par(json!([inner]))]))),
        "DSL.CTRL.013"
    );
}

#[test]
fn dsl_kern_011_machine_of_the_trace() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let g = parse(&graph(json!([vc_par(json!([load("xl1")]))])).to_string()).unwrap();
    program(&g, &m).unwrap();
    let mut other = g.clone();
    other.machine.mg = 4;
    assert_eq!(program(&other, &m).unwrap_err().clause, "DSL.KERN.011");
    assert_eq!(
        to_l2(&serde_json::to_string(&other).unwrap(), &m)
            .unwrap_err()
            .clause,
        "DSL.KERN.011"
    );
}

#[test]
fn dsl_graph_019_stage_gives_the_l2_program() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let g = graph(json!([vc_par(json!([load("xl1")]))]));
    let p = to_l2(&g.to_string(), &m).unwrap();
    assert_eq!(p.name, "k");
    let body = p.body().unwrap();
    assert_eq!(body.params.len(), 2);
    assert_eq!(body.stmts[0].op_name(), "par");
    let load = body.stmts[0].body()[0].as_op().unwrap();
    assert_eq!(load.target.as_deref(), Some("xl1"));
    assert_eq!(load.ty.as_ref().unwrap().to_string(), "f16[4, 128] @l1");
}

#[test]
fn dsl_idx_009_index_terms_read_as_the_tracers_spelling() {
    use ktpu_l1::text::{parse as read_text, Term};
    // A sum inside an atom is bracketed even when it starts with an atom:
    // `((a / 2) + 3) / 2`, not `(a / 2) + 3 / 2`.
    let inner = json!({"const": 3, "terms": [[{"floor": [var("a", 1), 2]}, 1]]});
    let slot = json!({"const": 0, "terms": [[{"floor": [var("k", 1), 7]}, 1], [{"var": "t"}, 1]]});
    for (x, spelt) in [
        (
            json!({"const": 0, "terms": [[{"floor": [inner, 2]}, 1]]}),
            "(((a / 2) + 3) / 2)",
        ),
        (
            json!({"const": 0, "terms": [[{"mod": [slot, 2]}, 200]]}),
            "(((k / 7) + t) % 2)*200",
        ),
        (
            json!({"const": 4, "terms": [[{"var": "t"}, -2]]}),
            "4 - t*2",
        ),
        (
            json!({"const": 0, "terms": [[{"var": "t"}, -2], [{"var": "u"}, -1]]}),
            "-t*2 - u",
        ),
        (json!(-3), "-3"),
    ] {
        let x: Index = serde_json::from_value(x).unwrap();
        assert_eq!(text::index(&x), spelt);
        let st = &read_text(&format!("op {spelt}"), "<t>").unwrap()[0];
        let want: &Term = &st.args[0];
        assert_eq!(&term::index(&x), want, "{spelt}");
    }
}

/// xorshift64: the generated indices are the same on every run.
struct Rng(u64);

impl Rng {
    fn next(&mut self, n: u64) -> i64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 % n) as i64
    }
}

const VARS: [&str; 3] = ["k", "t", "u"];

/// A nested index: 1 to 3 terms of a variable or a `/`, `%`, `*` atom over
/// smaller indices (positive int divisors), coefficients -3..3, a constant.
fn nested(r: &mut Rng, depth: u32) -> Index {
    let mut terms = vec![];
    for _ in 0..1 + r.next(3) {
        let atom = match if depth == 0 { 0 } else { r.next(4) } {
            0 => Atom::Var(VARS[r.next(3) as usize].into()),
            op => {
                let a = Box::new(nested(r, depth - 1));
                match op {
                    1 => Atom::Floor(a, Box::new(Index::Int(2 + r.next(7)))),
                    2 => Atom::Mod(a, Box::new(Index::Int(2 + r.next(7)))),
                    _ => Atom::Mul(a, Box::new(nested(r, depth - 1))),
                }
            }
        };
        let c = [1, 1, 2, 3, -1, -2, -3][r.next(7) as usize];
        terms.push((atom, c));
    }
    Index::Sym(Sym {
        konst: r.next(9) - 4,
        terms,
    })
}

/// The generated indices whose printed text reads to another value, with
/// the text; and how many were compared.
fn misread() -> (Vec<String>, usize) {
    let envs: Vec<Vec<(String, i64)>> = [[0, 1, 2], [13, 4, 7], [29, 9, 3]]
        .iter()
        .map(|vs| VARS.iter().map(|n| n.to_string()).zip(*vs).collect())
        .collect();
    let mut r = Rng(0x9e37_79b9_7f4a_7c15);
    let mut bad = vec![];
    let n = 400;
    for _ in 0..n {
        let x = nested(&mut r, 3);
        let s = text::index(&x);
        if envs
            .iter()
            .any(|e| index_read::read(&s, e) != index_read::value(&x, e))
        {
            bad.push(s);
        }
    }
    (bad, n)
}

/// Every generated nested index prints as text that reads to its own value
/// (DSL.IDX.009): 400 indices, three variable bindings each.
#[test]
fn dsl_idx_009_printed_indices_read_to_their_values() {
    let (bad, n) = misread();
    assert!(
        bad.is_empty(),
        "{} of {n} misread: {:?}",
        bad.len(),
        &bad[..bad.len().min(5)]
    );
}

/// The shortcut mutant (a sum starting with an atom unbracketed inside
/// another atom) misreads generated indices, and the clause spellings.
#[cfg(feature = "mutants")]
#[test]
fn dsl_idx_009_the_atom_sum_shortcut_misreads() {
    let (bad, n) = mutant::with(mutant::ATOM_SUM_SHORTCUT, misread);
    assert!(!bad.is_empty(), "the shortcut misread none of {n}");
    let spelt = mutant::with(mutant::ATOM_SUM_SHORTCUT, || {
        let slot =
            json!({"const": 0, "terms": [[{"floor": [var("k", 1), 7]}, 1], [{"var": "t"}, 1]]});
        let x: Index =
            serde_json::from_value(json!({"const": 0, "terms": [[{"mod": [slot, 2]}, 200]]}))
                .unwrap();
        text::index(&x)
    });
    assert_eq!(spelt, "((k / 7) + t % 2)*200");
}
