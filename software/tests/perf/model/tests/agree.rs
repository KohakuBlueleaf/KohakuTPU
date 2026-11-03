//! `ktpu_l2::cost` against the timed model over the node and memory anchors: both
//! price the node and mover from the same machine facts, so they agree within a
//! stated spread, and the flat-send pricing they replaced does not. The core
//! anchors (`vc.ktpu`) also read the timed model's own image corrections.

use std::path::{Path, PathBuf};

use ktpu_isa::Machine;
use ktpu_l1::text::read;
use ktpu_l2::cost::{self, FLAT_SEND_MUTANT};
use ktpu_l2::mutant::{scoped, State};
use ktpu_l2::target::Target;
use ktpu_perf::model::{kernel, Params};
use serde_json::Value;

/// The median and worst of |cost - (timed + launch_front)| / (timed + launch_front).
const MEDIAN: f64 = 0.05;
const WORST: f64 = 0.20;
const BOARD: &str = "multimesh_v9t6";
const ANCHORS: &[&str] = &["anchors/node.ktpu", "anchors/memory/dram.ktpu"];

fn perf() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

/// Each anchor kernel's relative difference, sorted.
fn spread() -> Vec<(f64, String)> {
    let root = perf().join("../../..");
    let m = Machine::load(root.join(format!("machines/{BOARD}.json"))).expect("the machine");
    let params: Value =
        serde_json::from_str(&std::fs::read_to_string(perf().join("params.json")).unwrap())
            .unwrap();
    let p = Params::of(&params, "value", &m);
    let front = params["launch_front"]["value"].as_f64().unwrap();
    let t = Target::of(&m);
    let mut out = Vec::new();
    for file in ANCHORS {
        let text = std::fs::read_to_string(perf().join(file)).unwrap();
        let module = read(&text, file).expect("an anchor module");
        for name in module.names() {
            let timed = kernel(&module, &name, &m, &p).expect("timed")["cycles"]
                .as_f64()
                .unwrap()
                + front;
            let cost = cost::cycles(&module, &name, &t).expect("cost") as f64;
            out.push(((cost - timed).abs() / timed, name));
        }
    }
    out.sort_by(|a, b| a.0.total_cmp(&b.0));
    out
}

fn median(s: &[(f64, String)]) -> f64 {
    s[s.len() / 2].0
}

#[test]
fn the_cost_model_agrees_with_the_timed_model() {
    let s = spread();
    assert!(s.len() >= 25, "{} anchors", s.len());
    let worst = s.last().unwrap();
    println!(
        "median {:.3} worst {:.3} ({})",
        median(&s),
        worst.0,
        worst.1
    );
    assert!(median(&s) <= MEDIAN && worst.0 <= WORST, "{s:?}");
}

#[test]
fn a_flat_send_leaves_the_spread() {
    let state = State {
        active: Some(FLAT_SEND_MUTANT),
        ..State::default()
    };
    let s = scoped(state, spread);
    println!("median {:.3} worst {:.3}", median(&s), s.last().unwrap().0);
    assert!(median(&s) > MEDIAN || s.last().unwrap().0 > WORST, "{s:?}");
}
