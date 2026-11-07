//! Complete FC2 schedules are selected by the same resource clock as reports.
use std::path::Path;

use ktpu_kernels::call_split;
use ktpu_kernels::split::{
    DispatchProfile, NodeSet, NumericContract, OutputRequirement, PlannedKernel, SplitRequest,
};
use ktpu_l2::{compile_l1, Search, PASSES};
use ktpu_machine::Machine;
use ktpu_perf::model::{kernel, Params};
use serde_json::{json, Value};

#[test]
fn joint_ownership_prices_less_work_than_replicated_weight_rows() {
    let perf = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("perf");
    let machine = Machine::load(perf.join("../../../machines/multimesh_v9t8.json"))
        .expect("four-sysnode machine");
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 197, "n": 768, "k": 3072,
            "pad_m": false, "dense_n": true
        }}},
        "e": {"nodes": [
            {"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]},
            {"load": 2}, {"op": "add", "args": [2, 3]}
        ], "root": 4},
        "extras": [
            {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 768, "rows": 200},
            {"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}
        ],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 768, "rows": 200},
        "ring_depth": 2, "tail_bands": true
    });
    let request = SplitRequest {
        nodes: NodeSet::new(vec![3, 1, 0, 2], &machine).expect("physical grant"),
        consumer: OutputRequirement::default(),
        dispatch: DispatchProfile::Common,
        numerics: NumericContract::default(),
        input_forms: vec![],
        force: None,
    };
    let choose = |force: Option<&str>| {
        let mut request = request.clone();
        request.force = force.map(str::to_owned);
        call_split("matmul.mm_kernel", &args, &request, &machine).expect("priced product")
    };
    let rows = choose(Some("m"));
    let selected = choose(None);
    assert_eq!(selected.split.active_nodes, request.nodes.ids().len());
    assert!(
        selected.split.cost.makespan < rows.split.cost.makespan,
        "selected {}: {:?}; rows: {:?}",
        selected.split.id,
        selected.split.cost,
        rows.split.cost,
    );
    assert!(selected.split.legality.numerics.reduction_order.is_empty());

    let params: Value = serde_json::from_str(
        &std::fs::read_to_string(perf.join("params.json")).expect("anchor parameters"),
    )
    .expect("parameter JSON");
    let params = Params::of(&params, "card", &machine);
    let report = |plan: &PlannedKernel| {
        let program = ktpu_graph::program(&plan.kernel.graph, &machine).expect("L2");
        let module = match &plan.l1 {
            Some(text) => ktpu_l1::text::read(text, "selected delivery").expect("L1 delivery"),
            None => {
                compile_l1(&program, &machine, &PASSES, true, &Search::default())
                    .expect("emitted schedule")
                    .l1
            }
        };
        let report = kernel(&module, &program.name, &machine, &params).expect("resource report");
        if plan.l1.is_none() {
            let expected = report["cycles"].as_f64().expect("completion").ceil() as i64
                + machine.vc.launch_front as i64;
            assert_eq!(plan.split.cost.makespan, expected);
            let nodes = report["node_cycles"]
                .as_object()
                .expect("physical completion");
            let expected_local: Vec<_> = request
                .nodes
                .ids()
                .iter()
                .enumerate()
                .filter(|(rank, _)| {
                    plan.split.shards.iter().any(|share| {
                        share.logical_node == *rank
                            && share.role != ktpu_kernels::split::ShardRole::Empty
                    })
                })
                .map(|(_, physical)| {
                    nodes[&physical.to_string()]
                        .as_f64()
                        .expect("owner completion")
                        .ceil() as i64
                })
                .collect();
            assert_eq!(plan.split.cost.local_cycles, expected_local);
        }
        report
    };
    let before = report(&rows);
    let after = report(&selected);
    let count = |value: &Value, key: &str| value[key].as_f64().expect("counter");
    assert!(count(&after, "cycles") < count(&before, "cycles"));
    // Distinct B owners and repeated same-owner loads both consume bandwidth.
    // The automatic objective is complete price, not exclusive B ownership.
    assert!(count(&after["work"], "read_bytes") < count(&before["work"], "read_bytes"));
}
