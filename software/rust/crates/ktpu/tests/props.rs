//! The properties the Python compile tests (`compiler/tests/kernel/*`) check
//! on the L1 a kernel compiles to, checked on the Rust stack's L1 from the
//! same inputs (`golden/props`, `golden/props_dump.py`: each test's own
//! kernel traced by the Python tracer, or its L2). Each property holds as
//! built and breaks under a named mutant (`KILLERS`).

#![cfg(feature = "stack")]

use ktpu::{Backend, Stack};
use ktpu_machine::{Machine, Refusal};
use regex::Regex;
use serde_json::Value;
use std::path::Path;

fn case(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/props")
        .join(format!("{name}.json"));
    let text = std::fs::read_to_string(&path).expect("golden/props: run golden/props_dump.py");
    serde_json::from_str(&text).unwrap()
}

/// The case's L1 text: a graph or L2 through every stage, or (`lower`) L2
/// lowered with no passes, as the Python test lowers it.
fn l1(name: &str) -> Result<String, Refusal> {
    let c = case(name);
    let m = Machine::named(c["machine"].as_str().unwrap()).unwrap();
    let n = c["nodes"].as_u64().unwrap() as u32;
    let m = match m.nodes() == n {
        true => m,
        false => m.with_nodes(n).unwrap(),
    };
    let input = c["input"].as_str().unwrap();
    let fn_name = c["name"].as_str().unwrap();
    let b = Stack;
    let p = match c["kind"].as_str().unwrap() {
        "graph" => b.graph_to_l2(input, &m)?,
        _ => b.parse_l2(input)?,
    };
    if c["kind"] == "lower" {
        return Ok(b.print_l1(&b.lower(&p, fn_name, &m)?));
    }
    let l1 = b.schedule(&b.lower(&b.passes(&p, fn_name, &m)?, fn_name, &m)?, &m)?;
    Ok(b.print_l1(&l1))
}

fn l2_of(name: &str) -> String {
    let c = case(name);
    let m = Machine::named(c["machine"].as_str().unwrap()).unwrap();
    Stack.print_l2(&Stack.graph_to_l2(c["input"].as_str().unwrap(), &m).unwrap())
}

fn re(p: &str) -> Regex {
    Regex::new(p).unwrap()
}

/// The node-level lines of `node n` (unit words dropped).
fn node_lines(l1: &str, n: u32) -> Vec<String> {
    let (mut out, mut inside) = (Vec::new(), false);
    let head = re(r"^    node \d+$");
    let line = re(r"^        \S");
    for ln in l1.lines() {
        if head.is_match(ln) {
            inside = ln.trim() == format!("node {n}");
            continue;
        }
        if inside && line.is_match(ln) {
            out.push(ln.trim().to_string());
        }
    }
    out
}

fn index(lines: &[String], s: &str) -> Option<usize> {
    lines.iter().position(|l| l == s)
}

/// One property: Ok, or why it does not hold.
type Prop = fn() -> Result<(), String>;

fn check(ok: bool, why: &str) -> Result<(), String> {
    ok.then_some(()).ok_or_else(|| why.to_string())
}

fn auto_takes_the_first_candidate_that_compiles() -> Result<(), String> {
    // rows candidates 1, 128, 64: the tracer (Python) refuses one row; the
    // first the stack compiles is 128.
    check(
        case("auto-rows-1")["kind"] == "trace-refused",
        "rows=1 traced",
    )?;
    l1("auto-rows-128")
        .map(|_| ())
        .map_err(|e| format!("rows=128 refused: {e}"))
}

/// Statistic producers and consumers compile through compact word storage.
fn statistics_load_and_store_sixteen_a_word() -> Result<(), String> {
    for case in ["bcast-sum-64", "bcast-sum-256", "stat-use"] {
        let text = l1(case).map_err(|e| format!("{case}: {e}"))?;
        check(
            text.contains("f16 n=1"),
            &format!("{case}: no compact word pack"),
        )?;
    }
    Ok(())
}

fn f32_rows_unpack_sixteen_words_a_register() -> Result<(), String> {
    let t = l1("f32-rows").map_err(|e| e.to_string())?;
    let at: Vec<i64> = re(r"(?m)vunpk v\d+ <- l1\[(\d+)\] f32$")
        .captures_iter(&t)
        .map(|c| c[1].parse().unwrap())
        .collect();
    let steps: std::collections::BTreeSet<i64> = at
        .iter()
        .take(8)
        .zip(at.iter().skip(1).take(7))
        .map(|(a, b)| b - a)
        .collect();
    check(at.len() >= 8 && steps == [16].into(), &format!("{at:?}"))
}

fn a_row_flip_is_one_copy_walking_back() -> Result<(), String> {
    let t = l1("move-flip").map_err(|e| e.to_string())?;
    check(
        t.contains("copy w(x + 3584, (8, -512), (16, 32)) -> w(y + 0, (128, 32))"),
        "no backward copy",
    )
}

fn a_window_fill_walks_the_destination() -> Result<(), String> {
    let t = l1("move-pad").map_err(|e| e.to_string())?;
    check(
        t.contains("fill w(y + 64, (8, 128), (2, 32)) value=15360 ew=16"),
        "no walked fill",
    )
}

fn a_walked_gt4_groups_four_source_words() -> Result<(), String> {
    let t = l1("move-tr").map_err(|e| e.to_string())?;
    check(
        re(r"gt4 w\(x \+ 0, \(16, 32\)\) -> w\(y \+ 0, \(4, 128\)\)").is_match(&t),
        "no walked gt4",
    )
}

fn xlane_is_a_multiply_by_one_through_the_crossbar() -> Result<(), String> {
    check(l2_of("swap").contains("xlane"), "no xlane in L2")?;
    let t = l1("swap").map_err(|e| e.to_string())?;
    check(
        re(r"vmul v\d+, k1, xor\(v\d+, 5\)").is_match(&t),
        "no vmul k1 xor",
    )
}

fn instances_of_one_shape_share_one_image() -> Result<(), String> {
    let t = l1("swap").map_err(|e| e.to_string())?;
    let runs: Vec<(String, String)> = re(r"run (\w+)\(([^)]*)\)")
        .captures_iter(&t)
        .map(|c| (c[1].to_string(), c[2].to_string()))
        .collect();
    check(
        t.matches("\nimage ").count() == 1
            && runs.len() == 2
            && runs[0].0 == runs[1].0
            && runs[0].1 != runs[1].1,
        &format!("runs {runs:?}"),
    )
}

fn the_l2_cross_node_split_k_lowers_to_the_handshake() -> Result<(), String> {
    let t = l1("xsplitk-2").map_err(|e| e.to_string())?;
    let (src, dst) = (node_lines(&t, 0), node_lines(&t, 1));
    let ring = index(&dst, "ring mesh=0 slot=9");
    let wait = index(&dst, "wait mg[0]");
    check(
        matches!((ring, wait), (Some(r), Some(w)) if r > w),
        "the target rings before it waits",
    )?;
    check(
        src.iter().any(|l| l == "wait slot 9 consume n=1"),
        "the source does not wait",
    )?;
    check(
        re(r"drain n=4096 to=mesh1\.mg\[0\] buf=2 l1=0").is_match(&t),
        "no add",
    )?;
    check(
        re(r"drain count n=4 to=mesh1\.mg\[0\] buf=0 l1=0").is_match(&t),
        "no mark",
    )?;
    check(t.contains("fill A0 recv n=1"), "no take")
}

fn the_dsl_split_k_adds_from_every_other_node_one_at_a_time() -> Result<(), String> {
    let t = l1("example-splitk-2").map_err(|e| e.to_string())?;
    let src = node_lines(&t, 1);
    let adds: Vec<usize> = src
        .iter()
        .enumerate()
        .filter(|(_, l)| l.starts_with("wait slot 8"))
        .map(|(i, _)| i)
        .collect();
    check(adds.len() == 2, &format!("{} adds", adds.len()))?;
    check(
        src[adds[0]..adds[1]].iter().any(|l| l == "wait mg[0]"),
        "the second add does not wait for the first",
    )
}

fn a_node_reading_another_nodes_writes_waits_for_its_ring() -> Result<(), String> {
    let t = l1("example-linear-2").map_err(|e| e.to_string())?;
    let (n0, n1) = (node_lines(&t, 0), node_lines(&t, 1));
    let ring = index(&n0, "ring mesh=1 slot=0").ok_or("node 0 never rings")?;
    check(
        n0[..ring].iter().any(|l| l.starts_with("wait mg[")),
        "node 0 rings before its writes land",
    )?;
    check(
        n1.first().map(String::as_str) == Some("wait slot 0 consume n=1"),
        "node 1 does not wait first",
    )
}

fn shared_b_fills_stay_within_what_a_follower_takes() -> Result<(), String> {
    let small = l1("bshare-128").map_err(|e| e.to_string())?;
    let large = l1("bshare-256").map_err(|e| e.to_string())?;
    check(small.contains("peers="), "64 entries not shared")?;
    check(!large.contains("peers="), "128 entries shared")?;
    match l1("bshare-256-cross") {
        Err(e) if e.message.contains("shared B fill of 128 entries") => Ok(()),
        Err(e) => Err(format!("refused otherwise: {e}")),
        Ok(_) => Err("a 128-entry shared fill compiles".into()),
    }
}

/// F-042 (rs-l2): three nodes adding into one cluster take turns, so the
/// checker finds no unordered bursts into its port (the 4-node splitk on the
/// v9t6 card; at 2 nodes one sender cannot race).
fn three_senders_into_one_cluster_add_in_turn() -> Result<(), String> {
    let c = case("example-splitk-4-v9t6");
    let m = Machine::named(c["machine"].as_str().unwrap())
        .and_then(|m| m.with_nodes(4))
        .map_err(|e| e.to_string())?;
    let input = ktpu::Input::Graph(c["input"].as_str().unwrap().to_string());
    let got =
        ktpu::compile(&Stack, &input, None, &m, &ktpu::Cache::off()).map_err(|e| e.to_string())?;
    let port = got
        .verdict
        .findings
        .iter()
        .filter(|f| f.kind == "port")
        .count();
    check(port == 0, &format!("{port} port findings"))
}

/// `test_vregs.py::test_pipeline_carries_the_machine_count`: the softmax L2
/// example through the pipeline on the 4+2 die with a 128-register core and
/// with its own 32: the L1 module's count and its first line.
fn the_pipeline_carries_the_machine_count() -> Result<(), String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let doc: Value = serde_json::from_str(
        &std::fs::read_to_string(root.join("machines/multimesh_v9.json")).unwrap(),
    )
    .unwrap();
    let l2 = std::fs::read_to_string(
        root.join("software/language/kohakutpu/language/examples/l2/vector/softmax.ktpu"),
    )
    .map_err(|e| e.to_string())?;
    for (regs, line) in [(128, "target ktpu.v9 vregs=128"), (32, "target ktpu.v9")] {
        let mut doc = doc.clone();
        doc["machine"]["vc"]["regs"] = regs.into();
        let m = Machine::from_json(&doc.to_string()).map_err(|e| e.to_string())?;
        let m = m.with_nodes(1).map_err(|e| e.to_string())?;
        let c = ktpu::compile(
            &Stack,
            &ktpu::Input::L2(l2.clone()),
            Some("softmax"),
            &m,
            &ktpu::Cache::off(),
        )
        .map_err(|e| e.to_string())?;
        let l1 = Stack.parse_l1(&c.l1).map_err(|e| e.to_string())?;
        check(
            l1.vregs == regs && c.l1.lines().next() == Some(line),
            &format!("{regs} registers: {} / {:?}", l1.vregs, c.l1.lines().next()),
        )?;
    }
    Ok(())
}

fn one_node_programs_carry_no_handshake() -> Result<(), String> {
    let t = l1("example-mlp-1").map_err(|e| e.to_string())?;
    check(
        !t.contains("ring mesh") && !t.contains("wait slot"),
        "a handshake on one node",
    )
}

/// Each property, its Python test, and the mutant that breaks it.
const PROPS: &[(&str, Prop, &str)] = &[
    (
        "auto_takes_the_first_candidate_that_compiles",
        auto_takes_the_first_candidate_that_compiles,
        KILL_AUTO,
    ),
    (
        "statistics_load_and_store_sixteen_a_word",
        statistics_load_and_store_sixteen_a_word,
        KILL_BCAST,
    ),
    (
        "f32_rows_unpack_sixteen_words_a_register",
        f32_rows_unpack_sixteen_words_a_register,
        KILL_F32,
    ),
    (
        "a_row_flip_is_one_copy_walking_back",
        a_row_flip_is_one_copy_walking_back,
        KILL_FLIP,
    ),
    (
        "a_window_fill_walks_the_destination",
        a_window_fill_walks_the_destination,
        KILL_PAD,
    ),
    (
        "a_walked_gt4_groups_four_source_words",
        a_walked_gt4_groups_four_source_words,
        KILL_TR,
    ),
    (
        "xlane_is_a_multiply_by_one_through_the_crossbar",
        xlane_is_a_multiply_by_one_through_the_crossbar,
        KILL_XLANE,
    ),
    (
        "instances_of_one_shape_share_one_image",
        instances_of_one_shape_share_one_image,
        KILL_IMAGE,
    ),
    (
        "the_l2_cross_node_split_k_lowers_to_the_handshake",
        the_l2_cross_node_split_k_lowers_to_the_handshake,
        KILL_XSPLITK,
    ),
    (
        "the_dsl_split_k_adds_from_every_other_node_one_at_a_time",
        the_dsl_split_k_adds_from_every_other_node_one_at_a_time,
        KILL_SPLITK,
    ),
    (
        "a_node_reading_another_nodes_writes_waits_for_its_ring",
        a_node_reading_another_nodes_writes_waits_for_its_ring,
        KILL_RING,
    ),
    (
        "shared_b_fills_stay_within_what_a_follower_takes",
        shared_b_fills_stay_within_what_a_follower_takes,
        KILL_BSHARE,
    ),
    (
        "one_node_programs_carry_no_handshake",
        one_node_programs_carry_no_handshake,
        KILL_ONENODE,
    ),
    (
        "three_senders_into_one_cluster_add_in_turn",
        three_senders_into_one_cluster_add_in_turn,
        "l2.lower/adds-unordered",
    ),
    (
        "the_pipeline_carries_the_machine_count",
        the_pipeline_carries_the_machine_count,
        KILL_VREGS,
    ),
];

const KILL_VREGS: &str = "l2.target/vregs-default";

const KILL_AUTO: &str = "l2.vector/row-64";
const KILL_BCAST: &str = "l2.body/type-mode-dropped";
const KILL_F32: &str = "l2.buffer/no-f32";
const KILL_FLIP: &str = "l2.mover/tuples-unread";
const KILL_PAD: &str = "l2.mover/no-fill";
const KILL_TR: &str = "l2.mover/no-merge";
const KILL_XLANE: &str = "l2.vector/xlane-rot";
const KILL_IMAGE: &str = "l2.lower/no-shared-image";
const KILL_XSPLITK: &str = "l2.lower/added-into-ignored";
const KILL_SPLITK: &str = "l2.lower/far-adds-at-once";
const KILL_RING: &str = "l2.sync/never-clash";
const KILL_BSHARE: &str = "l2.lower/share-unpaired";
const KILL_ONENODE: &str = "l2.lower/handshake-always";

#[test]
fn every_property_holds() {
    let bad: Vec<String> = PROPS
        .iter()
        .filter_map(|(n, p, _)| p().err().map(|e| format!("{n}: {e}")))
        .collect();
    assert!(bad.is_empty(), "\n{}", bad.join("\n"));
}

#[cfg(feature = "mutants")]
#[test]
fn every_property_breaks_under_its_mutant() {
    let bad: Vec<String> = PROPS
        .iter()
        .filter(|(_, p, kill)| !broken(kill, *p))
        .map(|(n, _, kill)| format!("{n}: holds under {kill:?}"))
        .collect();
    assert!(bad.is_empty(), "\n{}", bad.join("\n"));
}

/// The one-node property is broken by a handshake, not by a failed compile:
/// under `l2.lower/handshake-always` the mlp compiles end to end (packages
/// and verdict) with a ring and a slot wait in its L1.
#[cfg(feature = "mutants")]
#[test]
fn a_one_node_handshake_compiles_and_breaks_the_property() {
    let got = ktpu::mutant::with(KILL_ONENODE, || {
        let c = case("example-mlp-1");
        let m = Machine::named(c["machine"].as_str().unwrap())
            .and_then(|m| m.with_nodes(1))
            .unwrap();
        let input = ktpu::Input::Graph(c["input"].as_str().unwrap().to_string());
        ktpu::compile(&Stack, &input, None, &m, &ktpu::Cache::off())
    })
    .unwrap();
    let c = got.unwrap_or_else(|e| panic!("the mutant's mlp does not compile: {e}"));
    assert!(!c.packages.is_empty() || c.bound.is_some(), "no packages");
    assert!(
        c.l1.contains("ring mesh") && c.l1.contains("wait slot"),
        "no handshake"
    );
}

/// Whether `p` fails (or the compile panics) under mutant `m`.
#[cfg(feature = "mutants")]
fn broken(m: &str, p: Prop) -> bool {
    let got = std::panic::catch_unwind(|| ktpu::mutant::with(m, p));
    !matches!(got, Ok(Ok(Ok(()))) | Ok(Err(_)))
}

/// Every mutant that breaks each property (run with `--ignored` to choose
/// `KILLERS`).
#[cfg(feature = "mutants")]
#[test]
#[ignore]
fn find_killers() {
    let only = std::env::var("KTPU_PROPS").unwrap_or_default();
    for (n, p, _) in PROPS.iter().filter(|(n, _, _)| n.contains(only.as_str())) {
        let killers: Vec<&str> = ktpu::mutant::names()
            .into_iter()
            .filter(|m| broken(m, *p))
            .collect();
        println!("{n}: {killers:?}");
    }
}
