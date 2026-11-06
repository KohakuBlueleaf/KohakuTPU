//! Kernel split contracts exercised by the DSL stage.

use std::collections::HashMap;
use std::path::Path;

use ktpu_kernels::call_split;
use ktpu_kernels::split::{
    choose_split, partition_tiles, validate_ownership, CostFeatures, DispatchProfile, LocalTiming,
    NodeSet, NumericContract, OutputLayout, OutputRequirement, Region, SplitRequest,
};
use ktpu_kernels::trace::{buffer::f16, Facts, Trace};
use ktpu_machine::Machine;
use ktpu_sim::l2::view::{Buf as SimBuf, Layout};
use ktpu_sim::num::mx7;
use ktpu_sim::Dram;
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

#[path = "attention.rs"]
mod attention;

fn simulate_matmul(kernel: &ktpu_kernels::kernel::Kernel) -> (Vec<u8>, Vec<u32>) {
    let text = ktpu_graph::lower(&kernel.graph).expect("L2 graph");
    let module = ktpu_l1::text::read(&text.text(), "matmul shares").expect("L2 text");
    let body = module.body(&text.name, "l2").expect("body");
    let mut binds = HashMap::new();
    let mut buffers = Vec::new();
    let mut end = 4096;
    for (name, ty) in &body.params {
        let b = SimBuf::of(ty, end).expect("physical buffer");
        binds.insert(name.clone(), end);
        end += (b.bytes() + 4095) / 4096 * 4096;
        buffers.push((name.clone(), b));
    }
    let mut mem = Dram::new(end as usize);
    for (name, b) in &buffers {
        if let Layout::Mx7 { b: side, .. } = b.layout {
            for group in 0..b.shape[0] / 4 {
                for block in 0..b.shape[1] / 32 {
                    let mut values = [[0u16; 32]; 4];
                    for (lane, row) in values.iter_mut().enumerate() {
                        for (k, value) in row.iter_mut().enumerate() {
                            let tag = group * 71
                                + block * 29
                                + lane as i64 * 13
                                + k as i64 * 3
                                + i64::from(side);
                            *value = 0x2400 | (tag as u16 & 0x3ff) | (((tag / 5) as u16 & 1) << 15);
                        }
                    }
                    let at = b.entry(group, block).expect("packed address");
                    for (word, bytes) in mx7::entry(&values).write(side).iter().enumerate() {
                        mem.set_word(at + word as i64 * 32, bytes)
                            .expect("packed input");
                    }
                }
            }
        } else if name != "out" {
            for i in 0..b.bytes() / 2 {
                let code = 0x2800u16 | ((i * 37) as u16 & 0x3ff);
                mem.write(b.at + 2 * i, &code.to_le_bytes())
                    .expect("epilogue input");
            }
        }
    }
    let (_, out) = buffers.iter().find(|(n, _)| n == "out").expect("output");
    let start = out.at as usize;
    mem.watch_writes(start..start + out.bytes() as usize)
        .expect("watch the output");
    ktpu_sim::l2::run(&module, &text.name, &binds, &mut mem, 7).expect("simulated shares");
    let bytes = mem.read(out.at, out.bytes() as usize).expect("C").to_vec();
    (bytes, mem.write_counts().expect("write census").to_vec())
}

#[test]
fn matmul_output_splits_preserve_k_order_and_write_each_word_once() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 197, "n": 192, "k": 64,
            "n_tile": "auto", "waves": true
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 192, "rows": 200},
        "ring_depth": 2, "tail_bands": true
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("P=1");
    let (want, _) = simulate_matmul(&baseline);
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let p = grant.len();
        let mut req = request(&m);
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        // P=2 covers both 1-D axes; P=4 the one 2-D factorisation (mn-1xP is N, mn-Px1 is M).
        let ids: Vec<String> = match p {
            2 => vec!["m".into(), "n".into()],
            _ => vec!["mn-2x2".into()],
        };
        for id in ids {
            req.force = Some(id.clone());
            let got = call_split("matmul.mm_kernel", &args, &req, &m).expect("forced split");
            validate_ownership(&got.split.output).expect("physical owners");
            assert_eq!(got.split.active_nodes, p, "{id}: grant is useful work");
            let (actual, writes) = simulate_matmul(&got.kernel);
            let valid = 197 * 192 * 2;
            assert_eq!(&actual[..valid], &want[..valid], "{p} nodes, {id}");
            for (word, counts) in writes[..valid].chunks(32).enumerate() {
                assert!(
                    counts.iter().all(|&n| n == 1),
                    "{p} nodes, {id}, word {word}: {counts:?}"
                );
            }
        }
    }
}

#[test]
fn matmul_row_reduction_keeps_complete_rows_and_statistic_origins() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 197, "n": 32, "k": 256, "quantum": 16,
            "n_tile": "auto", "waves": true
        }}},
        "e": {"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1},
        "extras": [],
        "out": {"kind": "row", "dtype": "f16", "offset": 32, "pitch": 0, "rows": 240},
        "ring_depth": 2, "tail_bands": true
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("P=1 rows");
    let (want, baseline_writes) = simulate_matmul(&baseline);
    assert!(
        baseline_writes[..64].iter().all(|&n| n == 0),
        "statistic prefix"
    );
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("n".into());
    assert!(call_split("matmul.mm_kernel", &args, &req, &m).is_err());
    req.force = Some("mn-2x2".into());
    assert!(call_split("matmul.mm_kernel", &args, &req, &m).is_err());
    req.force = Some("m".into());
    let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("whole-row owners");
    let (actual, writes) = simulate_matmul(&plan.kernel);
    assert_eq!(&actual[64..64 + 197 * 2], &want[64..64 + 197 * 2]);
    assert!(writes[..64].iter().all(|&n| n == 0), "statistic prefix");
    assert!(
        writes[64..64 + 197 * 2].iter().all(|&n| n == 1),
        "one statistic owner"
    );
}

#[test]
fn matmul_partial_lines_have_one_destination_owner() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 20, "n": 192, "k": 64,
            "n_tile": "auto", "waves": true
        }}},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 16, "pitch": 224, "rows": 20}
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("word-aligned view");
    let (want, _) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.force = Some("n".into());
    let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("boundary owners");
    validate_ownership(&plan.split.output).expect("one owner per physical line");
    assert!(
        plan.split.conversions > 0,
        "cross-line share needs private output"
    );
    let (actual, writes) = simulate_matmul(&plan.kernel);
    for row in 0..20 {
        let lo = (row * 224 + 16) * 2;
        let hi = lo + 192 * 2;
        assert_eq!(&actual[lo..hi], &want[lo..hi]);
        assert!(writes[lo..hi].iter().all(|&n| n == 1));
        assert!(writes[row * 448..lo].iter().all(|&n| n == 0));
        assert!(writes[hi..(row + 1) * 448].iter().all(|&n| n == 0));
    }
}

#[test]
fn matmul_class_splits_keep_counted_products_and_global_inputs() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 2, "m": 197, "n": 192, "k": 64, "shared": false,
            "n_tile": "auto", "waves": true
        }}},
        "shape_class": {"kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 192, "rows": 400}
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("class P=1");
    let (want, _) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    for id in ["m", "n", "mn-2x2"] {
        req.force = Some(id.into());
        let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("class split");
        let text = ktpu_graph::lower(&plan.kernel.graph)
            .expect("class graph")
            .text();
        assert!(text.contains("gemm_tiles"), "class counted product: {text}");
        let (actual, writes) = simulate_matmul(&plan.kernel);
        assert_eq!(actual, want, "{id}: batch-specific A and B");
        assert!(
            writes.iter().all(|&n| n == 1),
            "{id}: one class output owner"
        );
    }
}

#[test]
fn matmul_empty_shares_issue_no_work() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 4, "n": 32, "k": 32
        }}},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 4}
    });
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("one tile");
    let (want, _) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("m".into());
    let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("empty owners");
    let program = ktpu_graph::program(&plan.kernel.graph, &m).expect("graph");
    let body = program.body().expect("body");
    assert!(ktpu_l2::ir::instances(&body)
        .expect("instances")
        .iter()
        .all(|s| s.node == 3));
    let (actual, writes) = simulate_matmul(&plan.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}
