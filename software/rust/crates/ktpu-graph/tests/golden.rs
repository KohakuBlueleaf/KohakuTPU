//! The goldens of `software/rust/golden/graph` (`graph_dump.py`) and every
//! traced case of `software/rust/golden/e2e` (`dump.py`): the graph reads back
//! to the same JSON, the program built from it is, as a tree, the program
//! ktpu-l2 reads from the tracer's text, and the text dump is that text.

use std::path::{Path, PathBuf};

use ktpu_graph::{build, file_of, lower, parse, to_l2, Graph};
use ktpu_l2::Program;
use ktpu_machine::Machine;
use serde_json::Value;

fn dir(name: &str) -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden")
        .join(name);
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

fn doc(f: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap()
}

fn graph(f: &Path, doc: &Value) -> Graph {
    let g = parse(&doc["graph"].to_string()).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
    assert_eq!(
        serde_json::to_value(&g).unwrap(),
        doc["graph"],
        "{}: the graph does not read back whole",
        f.display()
    );
    g
}

/// A program as a tree: the module's target and register file, the fn's
/// parameters and scratch, each node's op, target, type, terms and block.
fn tree(p: &Program) -> String {
    let body = p
        .body()
        .unwrap_or_else(|e| panic!("ktpu-l2 refuses {}: {e}", p.name));
    let nodes: Vec<String> = body.stmts.iter().map(|n| node(n)).collect();
    format!(
        "{:?} {} {} {} {:?} {:?}\n{}",
        p.module.target,
        p.module.vregs,
        p.module.vcopies,
        body.name,
        body.params,
        body.scratch,
        nodes.join("\n")
    )
}

fn node(n: &ktpu_l2::Node) -> String {
    let st = n.st();
    let kids: Vec<String> = n.body().iter().map(|k| node(k)).collect();
    format!(
        "{} {:?} {:?} {:?} {:?} [{}]",
        n.op_name(),
        n.target(),
        n.ty(),
        st.args,
        st.annot,
        kids.join(", ")
    )
}

#[test]
fn every_graph_golden_builds_the_program_of_the_tracers_text() {
    let files = dir("graph");
    assert!(files.len() >= 80, "{} goldens", files.len());
    let mut bad = Vec::new();
    for f in &files {
        let d = doc(f);
        let g = graph(f, &d);
        let want = Program::parse(d["l2"].as_str().unwrap(), &file_of(&g), Some(&g.name))
            .unwrap_or_else(|e| panic!("{}: ktpu-l2 refuses the tracer's text: {e}", f.display()));
        let got = build(&g).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        let (want, got) = (tree(&want), tree(&got));
        if want != got {
            bad.push(format!(
                "{}:\n--- want\n{want}\n--- got\n{got}",
                f.display()
            ));
        }
    }
    assert!(
        bad.is_empty(),
        "{} of {} differ:\n{}",
        bad.len(),
        files.len(),
        bad.join("\n")
    );
}

#[test]
fn every_graph_golden_dumps_the_tracers_text() {
    let mut bad = Vec::new();
    for f in &dir("graph") {
        let d = doc(f);
        let got = lower(&graph(f, &d)).unwrap().text();
        if got != d["l2"].as_str().unwrap() {
            bad.push(f.display().to_string());
        }
    }
    assert!(bad.is_empty(), "the text dump differs: {bad:?}");
}

#[test]
fn every_e2e_graph_is_the_stage_input_of_its_l2() {
    let mut bad = Vec::new();
    let mut seen = 0;
    for f in dir("e2e") {
        let d = doc(&f);
        if d["graph"].is_null() {
            continue;
        }
        seen += 1;
        let g = graph(&f, &d);
        let m = Machine::named(d["machine"].as_str().unwrap()).unwrap();
        let got =
            to_l2(&d["graph"].to_string(), &m).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        let t = ktpu_l2::Target::of(&m);
        let want = Program::parse(d["l2"].as_str().unwrap(), &file_of(&g), Some(&g.name))
            .unwrap()
            .with_vregs(t.vregs, t.vcopies);
        if tree(&want) != tree(&got) {
            bad.push(f.display().to_string());
        }
    }
    assert!(seen >= 50, "{seen} traced e2e cases");
    assert!(bad.is_empty(), "{} of {seen} differ: {bad:?}", bad.len());
}

/// The stage's register file is the machine's vector core's (2p.3: the
/// machine file is its only source): a core of other `regs` / `rf_copies`
/// changes the program's, and `build` keeps the text's default.
#[test]
fn the_register_file_comes_from_the_machine() {
    let f = &dir("e2e")
        .into_iter()
        .find(|f| !doc(f)["graph"].is_null())
        .expect("a traced e2e case");
    let d = doc(f);
    let g = graph(f, &d);
    let m = Machine::named(d["machine"].as_str().unwrap()).unwrap();
    let mut seen = Vec::new();
    for (regs, copies) in [(32, 4), (64, 3), (128, 2)] {
        let m = m
            .with_fact("vc.regs", regs.into())
            .and_then(|m| m.with_fact("vc.rf_copies", copies.into()))
            .unwrap();
        let p = ktpu_graph::program(&g, &m).unwrap();
        assert_eq!((p.module.vregs, p.module.vcopies), (regs, copies));
        seen.push(tree(&p));
    }
    seen.dedup();
    assert_eq!(seen.len(), 3, "the register file shows in the program");
    let text = build(&g).unwrap();
    let d0 = ktpu_l1::text::Module::default();
    assert_eq!(
        (text.module.vregs, text.module.vcopies),
        (d0.vregs, d0.vcopies)
    );
}
