//! Kernel split contracts exercised by the DSL stage.

use std::path::Path;

use ktpu_kernels::call_split;
use ktpu_kernels::split::{
    choose_split, partition_tiles, validate_ownership, CostFeatures, DispatchProfile, LocalTiming,
    NodeSet, NumericContract, OutputLayout, OutputRequirement, Region, SplitRequest,
};
use ktpu_kernels::trace::{buffer::f16, Facts, Trace};
use ktpu_machine::Machine;
use serde_json::json;

fn machine() -> Machine {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    Machine::load(root.join("machines/multimesh_v9t8.json")).expect("machine")
}

#[test]
fn remainder_partitions_write_every_tile_once() {
    for (tiles, participants) in [(2, 4), (11, 4)] {
        let shares = partition_tiles(tiles, participants).expect("partition");
        let mut writes = vec![0; tiles as usize];
        for share in shares {
            for tile in share {
                writes[tile as usize] += 1;
            }
        }
        assert!(writes.iter().all(|&n| n == 1), "{writes:?}");
    }
}

#[test]
fn ownership_rejects_disjoint_bytes_in_one_line_and_missing_output() {
    let region = |bytes| Region {
        allocation: "out".into(),
        bytes,
    };
    let mut layout = OutputLayout {
        valid_region: vec![region(0..128)],
        ownership: vec![(0, region(0..32)), (1, region(32..128))],
        ..Default::default()
    };
    assert!(validate_ownership(&layout).is_err());
    layout.ownership = vec![(0, region(0..64)), (1, region(64..128))];
    validate_ownership(&layout).expect("whole line owners");
    layout.ownership.pop();
    assert!(validate_ownership(&layout).is_err());
}

fn request(m: &Machine) -> SplitRequest {
    SplitRequest {
        nodes: NodeSet::new(vec![3, 1], m).expect("grant"),
        consumer: OutputRequirement::default(),
        dispatch: DispatchProfile::Common,
        numerics: NumericContract::default(),
        input_forms: vec![],
        force: None,
    }
}

#[test]
fn compatibility_share_executes_on_the_first_granted_node() {
    let m = machine();
    for grant in [vec![3], vec![3, 1], vec![3, 1, 0, 2]] {
        let mut request = request(&m);
        request.nodes = NodeSet::new(grant, &m).expect("grant");
        let plan =
            call_split("convert.copy_kernel", &json!({"nbytes": 128}), &request, &m).expect("plan");
        let program = ktpu_graph::program(&plan.kernel.graph, &m).expect("explicit node graph");
        let body = program.body().expect("body");
        let instances = ktpu_l2::ir::instances(&body).expect("instances");
        assert!(!instances.is_empty());
        assert!(instances.iter().all(|s| s.node == 3));
        assert_eq!(plan.split.active_nodes, 1, "only emitted work counts");
    }
    let ordinary = ktpu_kernels::call("convert.copy_kernel", &json!({"nbytes": 128}), &m)
        .expect("ordinary call");
    let ordinary = ktpu_graph::program(&ordinary.graph, &m).expect("ordinary graph");
    let body = ordinary.body().expect("body");
    assert!(ktpu_l2::ir::instances(&body)
        .expect("instances")
        .iter()
        .all(|s| s.node == 0));
}

#[test]
fn explicit_shares_keep_the_granted_physical_ids() {
    let m = machine();
    let request = request(&m);
    let mut plan =
        call_split("convert.copy_kernel", &json!({"nbytes": 128}), &request, &m).expect("plan");
    let mut trace = Trace::new("shares", Facts::of(&m), &m.die.target);
    let spec = f16(&[4, 16]).expect("buffer");
    let src = trace
        .port("src", ktpu_graph::Role::In, spec.clone())
        .expect("src");
    let a = trace
        .port("a", ktpu_graph::Role::Out, spec.clone())
        .expect("a");
    let b = trace.port("b", ktpu_graph::Role::Out, spec).expect("b");
    let outputs = [a, b];
    for share in &mut plan.split.shards {
        share.role = ktpu_kernels::split::ShardRole::Compute;
    }
    ktpu_kernels::split::emit_shares(
        &mut trace,
        &request.nodes,
        &plan.split.shards,
        |t, share| {
            t.copy(
                &outputs[share.logical_node].whole(),
                &src.whole(),
                None,
                None,
            )
        },
    )
    .expect("shares");
    let graph = trace.graph().expect("graph");
    let program = ktpu_graph::program(&graph, &m).expect("node graph");
    let body = program.body().expect("body");
    let instances = ktpu_l2::ir::instances(&body).expect("instances");
    let nodes: Vec<_> = instances.iter().map(|s| s.node).collect();
    assert_eq!(nodes, request.nodes.ids());
}

#[test]
fn compatibility_refuses_incompatible_output_and_numeric_requirements() {
    let m = machine();
    let mut request = request(&m);
    request.consumer.accepted_forms = vec!["other".into()];
    assert!(call_split("convert.copy_kernel", &json!({"nbytes": 128}), &request, &m).is_err());
    request.consumer = OutputRequirement::default();
    request.numerics.reduction_order = vec![(0, 1)];
    assert!(call_split("convert.copy_kernel", &json!({"nbytes": 128}), &request, &m).is_err());
}

#[test]
fn distributed_composition_charges_its_conversion_sequence() {
    let m = machine();
    let mut producer = call_split(
        "convert.copy_kernel",
        &json!({"nbytes": 128}),
        &request(&m),
        &m,
    )
    .expect("plan");
    let mut conversion = producer.clone();
    let mut materialized = producer.clone();
    producer.split.cost.makespan = 10;
    conversion.split.cost.makespan = 100;
    materialized.split.cost.makespan = 50;
    let mode = ktpu_kernels::gen::prepared::Composed::Cost;
    assert!(ktpu_kernels::composed::split_sequences(
        mode,
        std::slice::from_ref(&producer),
        std::slice::from_ref(&materialized),
        &m,
    ));
    assert!(!ktpu_kernels::composed::split_sequences(
        mode,
        &[producer, conversion],
        &[materialized],
        &m,
    ));
}

#[test]
fn selection_prices_the_slowest_share_and_forcing_keeps_its_price() {
    let m = machine();
    let base = call_split(
        "convert.copy_kernel",
        &json!({"nbytes": 128}),
        &request(&m),
        &m,
    )
    .expect("plan")
    .split;
    let candidate = |id: &str, cycles: Vec<i64>, conversions| {
        let mut option = base.clone();
        option.id = id.into();
        option.active_nodes = cycles.len();
        option.conversions = conversions;
        (
            option,
            CostFeatures {
                share_cycles: cycles,
                dispatch_intercept: 10,
            },
        )
    };
    let options = vec![
        candidate("slow", vec![1, 90], 0),
        candidate("convert", vec![50, 50], 1),
        candidate("z", vec![50, 50], 0),
        candidate("a", vec![50, 50], 0),
    ];
    let chosen = choose_split(options.clone(), &LocalTiming, None).expect("selection");
    assert_eq!(chosen.id, "a");
    let forced = choose_split(options, &LocalTiming, Some("slow")).expect("forced");
    assert!(forced.cost.makespan > chosen.cost.makespan);
}
