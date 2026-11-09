//! Kernel split contracts exercised by the DSL stage.

#[path = "../common.rs"]
mod common;

use std::collections::{BTreeMap, HashMap};
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
fn destination_copy_preserves_global_origins_and_single_word_writers() {
    let m = machine();
    for (nbytes, classed) in [(289, false), (289, true)] {
        let mut args = json!({"nbytes": nbytes});
        if classed {
            args["shape_class"] = json!({
                "kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"
            });
        }
        let baseline = ktpu_kernels::call("convert.copy_kernel", &args, &m).expect("P=1");
        let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
        let elements = baseline
            .graph
            .ports
            .iter()
            .find(|p| p.name == "dst")
            .expect("destination")
            .spec
            .shape
            .iter()
            .product::<i64>();
        let want: Vec<_> = (0..elements).flat_map(|i| input(i).to_le_bytes()).collect();
        for grant in [vec![3], vec![3, 1, 0, 2]] {
            let mut request = request(&m);
            request.nodes = NodeSet::new(grant, &m).expect("grant");
            request.force = Some("destination-tiles".into());
            let plan = call_split("convert.copy_kernel", &args, &request, &m).expect("plan");
            validate_ownership(&plan.split.output).expect("physical owners");
            let allocation_bytes = want.len() as u64;
            for share in &plan.split.shards {
                for read in &share.read_regions {
                    assert!(read.bytes.end <= allocation_bytes);
                }
                if classed && share.role != ktpu_kernels::split::ShardRole::Empty {
                    assert!(share.read_regions.iter().any(|read| {
                        read.allocation == "src" && read.bytes.end == allocation_bytes
                    }));
                }
            }
            let (actual, writes) =
                simulate_named_inputs(&plan.kernel, "dst", &|_, _, _, _, _| 0, &input);
            assert_eq!(actual, want);
            assert!(writes.iter().all(|&count| count == 1));
            let program = ktpu_graph::program(&plan.kernel.graph, &m).expect("node graph");
            let body = program.body().expect("body");
            let instances = ktpu_l2::ir::instances(&body).expect("instances");
            for share in &plan.split.shards {
                let node = request.nodes.ids()[share.logical_node];
                assert_eq!(
                    instances.iter().any(|s| s.node == node),
                    share.role != ktpu_kernels::split::ShardRole::Empty,
                );
            }
            assert_eq!(
                plan.split.active_nodes,
                request.nodes.ids().len().min((nbytes + 63) as usize / 64),
            );
        }
    }
    for classed in [true, false] {
        let width = 32;
        let src_rows = 16;
        let (dst_rows, dst_pitch, src_first, dst_first, rows, copied) = if classed {
            (18, 64, 3, 1, 16, 13)
        } else {
            (13, 32, 0, 0, 13, 13)
        };
        let generator = if classed {
            "fused.kernel"
        } else {
            "convert.rows_kernel"
        };
        let src_name = if classed { "input" } else { "src" };
        let dst_name = if classed { "result" } else { "dst" };
        let args = if classed {
            json!({
                "ports": [
                    {"name": src_name, "shape": [src_rows, width], "dtype": "f16", "written": false},
                    {"name": dst_name, "shape": [dst_rows, dst_pitch], "dtype": "f16", "written": true}
                ], "scratch": [], "staged": [],
                "steps": [
                    {"kind": "prefix", "src": src_name, "dst": dst_name, "boundary": {
                        "src_rows": src_rows, "dst_rows": dst_rows, "width": width,
                        "src_pitch": width, "dst_pitch": dst_pitch,
                        "src_first": src_first, "dst_first": dst_first,
                        "rows": rows, "copied_rows": copied, "classed": true
                    }},
                    {"kind": "prefix", "src": src_name, "dst": dst_name, "boundary": {
                        "src_rows": src_rows, "dst_rows": dst_rows, "width": width,
                        "src_pitch": width, "dst_pitch": dst_pitch,
                        "dst_first": dst_first + copied - 1, "rows": 1,
                        "copied_rows": 0, "initialize": true
                    }}
                ]
            })
        } else {
            json!({"src_rows": src_rows, "dst_rows": dst_rows, "width": width})
        };
        if classed {
            let mut aliased = args.clone();
            aliased["steps"][0]["src"] = json!(dst_name);
            aliased["steps"][0]["boundary"]["src_rows"] = json!(dst_rows);
            aliased["steps"][0]["boundary"]["src_pitch"] = json!(dst_pitch);
            let error = ktpu_kernels::call("fused.kernel", &aliased, &m)
                .expect_err("initialization cannot erase a copied source");
            assert_eq!(error.clause, "API.OP.001");
        }
        let source = f16(&[src_rows, width]).expect("source rows");
        let destination = f16(&[dst_rows, dst_pitch]).expect("destination rows");
        let mut t = Trace::new("row_boundary_reference", Facts::of(&m), &m.die.target);
        let src = t
            .port(src_name, ktpu_graph::Role::In, source.clone())
            .expect("src");
        let dst = t
            .port(dst_name, ktpu_graph::Role::Out, destination.clone())
            .expect("dst");
        let walk = [(copied * width / 16, 32)];
        if classed {
            let rectangle = dst
                .at(&[
                    ktpu_kernels::trace::buffer::sl(dst_first, dst_first + rows),
                    ktpu_kernels::trace::buffer::sl(0, width),
                ])
                .expect("destination prefix");
            t.fill_value(&rectangle, 0, None, None)
                .expect("initialize capacity prefix");
            let destination_walk = [(copied, dst_pitch * 2), (width / 16, 32)];
            t.copy_classed(
                &dst.whole(),
                &src.whole(),
                (&walk, Some(src_first * width * 2)),
                (&destination_walk, Some(dst_first * dst_pitch * 2)),
                Some(1),
            )
            .expect("pitched logical prefix");
            t.fill_value(
                &dst.at(&[
                    ktpu_kernels::trace::buffer::sl(dst_first + copied - 1, dst_first + copied),
                    ktpu_kernels::trace::buffer::sl(0, width),
                ])
                .expect("cleared row"),
                0,
                None,
                None,
            )
            .expect("clear final copied row");
        } else {
            t.copy(
                &dst.whole(),
                &src.whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )
            .expect("logical prefix");
        }
        let reference = ktpu_kernels::kernel::Kernel {
            graph: t.graph().expect("explicit boundary"),
            given: vec![(src_name.into(), source), (dst_name.into(), destination)],
        };
        let local = ktpu_kernels::call(generator, &args, &m).expect("row boundary");
        assert_eq!(
            ktpu_graph::cycles(&local.graph, &m).expect("typed boundary cycles"),
            ktpu_graph::cycles(&reference.graph, &m).expect("explicit boundary cycles"),
        );
        let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
        let touched = |i| {
            let row = i / dst_pitch;
            row >= dst_first && row < dst_first + rows && i % dst_pitch < width
        };
        let expected: Vec<_> = (0..dst_rows * dst_pitch)
            .flat_map(|i| {
                let row = i / dst_pitch - dst_first;
                let col = i % dst_pitch;
                let value = if !touched(i) {
                    0x5a5a
                } else if row < copied - i64::from(classed) {
                    input((src_first + row) * width + col)
                } else {
                    0
                };
                value.to_le_bytes()
            })
            .collect();
        let seeded: Vec<_> = (0..dst_rows * dst_pitch)
            .flat_map(|_| 0x5a5au16.to_le_bytes())
            .collect();
        let check = |actual: Vec<u8>, writes: Vec<u32>| {
            assert_eq!(actual, expected);
            for (byte, count) in writes.iter().enumerate() {
                assert_eq!(*count > 0, touched(byte as i64 / 2));
            }
        };
        let (actual, writes) = simulate_named_tables(
            &local,
            dst_name,
            &|_, _, _, _, _| 0,
            &input,
            &[(dst_name, seeded.clone())],
        );
        check(actual, writes);
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("boundary owners");
        req.force = Some(if classed { "rows" } else { "destination-tiles" }.into());
        let plan = call_split(generator, &args, &req, &m).expect("distributed boundary");
        assert_eq!(plan.split.active_nodes, 4);
        validate_ownership(&plan.split.output).expect("boundary ownership");
        for share in &plan.split.shards {
            for region in &share.read_regions {
                assert_eq!(region.allocation, src_name);
                assert!(region.bytes.end <= (src_rows * width * 2) as u64);
            }
        }
        let (actual, writes) = simulate_named_tables(
            &plan.kernel,
            dst_name,
            &|_, _, _, _, _| 0,
            &input,
            &[(dst_name, seeded)],
        );
        check(actual, writes);
    }
    let copy = json!({"gen": "convert.copy_kernel", "args": {"nbytes": 128}});
    let sequence = ktpu_kernels::call(
        "kernel.sequence",
        &json!({"members": [copy.clone(), copy]}),
        &m,
    )
    .expect("ordered typed sequence");
    ktpu_graph::program(&sequence.graph, &m).expect("hygienic sequence graph");
    assert!(ktpu_kernels::call("kernel.sequence", &json!({"members": []}), &m).is_err());
    assert!(ktpu_kernels::call(
        "kernel.sequence",
        &json!({"members": [{
            "gen": "convert.copy_kernel", "args": {"nbytes": 128, "quant_backend": "vector_mx7"}
        }]}),
        &m
    )
    .is_err());
    assert!(ktpu_kernels::call(
        "convert.prefix_kernel",
        &json!({
            "src_rows": 4, "dst_rows": 4, "width": 32, "rows": 4, "dst_first": 1
        }),
        &m
    )
    .is_err());
    // Reuse the two removed trivial-copy baseline simulations for a complete
    // global-input/product/global-output boundary composition.
    let product = json!({"gen": "matmul.mm_kernel", "args": {
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 16, "n": 32, "k": 32,
            "pad_m": false, "dense_n": true, "n_tiles": 1
        }}},
        "activation_any": true, "ring_depth": 1,
        "extras": [],
        "e": {"nodes": [{"load": 0}, {"const": 0.5},
            {"op": "mul", "args": [0, 1]}], "root": 2},
        "out": {"kind": "full", "dtype": "f16", "pitch": 32, "rows": 16, "offset": 0}
    }});
    let input_boundary = json!({"gen": "convert.prefix_kernel", "args": {
        "src_rows": 20, "dst_rows": 16, "width": 32,
        "src_first": 4, "rows": 16, "initialize": false
    }});
    let output_boundary = json!({"gen": "convert.prefix_kernel", "args": {
        "src_rows": 16, "dst_rows": 32, "width": 32,
        "dst_first": 7, "rows": 13, "initialize": false
    }});
    let emitter = |descriptor: &serde_json::Value| {
        ktpu_kernels::call(
            descriptor["gen"].as_str().expect("generator"),
            &descriptor["args"],
            &m,
        )
        .expect("explicit stage")
    };
    let explicit_output = ktpu_kernels::gen::chain::kernel(
        emitter(&product),
        emitter(&output_boundary),
        "src",
        "out",
        false,
    )
    .expect("uncontracted output boundary");
    let explicit = ktpu_kernels::gen::chain::kernel(
        emitter(&input_boundary),
        explicit_output,
        "a",
        "dst",
        false,
    )
    .expect("uncontracted input boundary");
    let descriptor = json!({
        "first": input_boundary,
        "second": {"gen": "kernel.chain", "args": {
            "first": product, "second": output_boundary,
            "from": "src", "to": "out", "keep": false
        }},
        "from": "a", "to": "dst", "keep": false
    });
    let mut complete = descriptor.clone();
    complete["second"]["args"]["second"]["args"]["rows"] = json!(16);
    let direct =
        ktpu_kernels::call("kernel.chain", &complete, &m).expect("complete global row chunk");
    assert!(!direct.graph.body.iter().any(|stmt| matches!(
        stmt.node,
        ktpu_graph::Node::Mover {
            op: ktpu_graph::MoverOp::Copy,
            ..
        }
    )));
    ktpu_graph::program(&direct.graph, &m).expect("bounded complete chunk graph");
    let mut next_complete = complete.clone();
    next_complete["first"]["args"]["src_first"] = json!(0);
    next_complete["second"]["args"]["second"]["args"]["dst_first"] = json!(0);
    let lookahead = ktpu_kernels::call(
        "kernel.sequence",
        &json!({
            "members": [
                {"gen": "kernel.chain", "args": complete},
                {"gen": "kernel.chain", "args": next_complete}
            ],
            "pipeline_lookahead": true
        }),
        &m,
    )
    .expect("proved tail lookahead");
    ktpu_graph::program(&lookahead.graph, &m).expect("lookahead dependencies");
    // Aliased quant storage can keep the last product wave dependent. The
    // optional pass must retain the legal schedule rather than force a move.
    assert_eq!(lookahead.graph.ports, direct.graph.ports);
    let optimized = ktpu_kernels::call("kernel.chain", &descriptor, &m)
        .expect("input view and streamed global output");
    assert!(!optimized.graph.scratch.iter().any(|s| s.name == "dst"));
    assert!(optimized
        .graph
        .scratch
        .iter()
        .any(|s| s.name.contains("boundary_tail")));
    assert_eq!(
        optimized
            .graph
            .body
            .iter()
            .filter(|s| matches!(
                s.node,
                ktpu_graph::Node::Mover {
                    op: ktpu_graph::MoverOp::Copy,
                    ..
                }
            ))
            .count(),
        1
    );
    assert!(
        ktpu_graph::cycles(&optimized.graph, &m).expect("optimized complete price")
            <= ktpu_graph::cycles(&explicit.graph, &m).expect("explicit complete price")
    );
    let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
        let output = &kernel
            .graph
            .ports
            .iter()
            .find(|p| p.role == ktpu_graph::Role::Out)
            .expect("global output")
            .name;
        let sentinel: Vec<_> = (0..32 * 32).flat_map(|_| 0x5a5au16.to_le_bytes()).collect();
        simulate_named_tables(
            kernel,
            output,
            &|_, _, _, _, k| 0x3400u16 + (k % 7) as u16,
            &|i| 0x2800u16 | ((i * 37) as u16 & 0x3ff),
            &[(output.as_str(), sentinel)],
        )
    };
    let mut pitched_descriptor = descriptor.clone();
    pitched_descriptor["first"]["args"]["dst_pitch"] = json!(64);
    pitched_descriptor["link_view"] = json!("prefix");
    let pitched = ktpu_kernels::call("kernel.chain", &pitched_descriptor, &m)
        .expect("pitched activation prefix link");
    assert!(!pitched.graph.scratch.iter().any(|s| s.name == "dst"));
    assert_eq!(
        pitched.graph.body, optimized.graph.body,
        "a single bounded quant reads the original logical prefix, not its capacity copy"
    );
    assert!(
        ktpu_graph::cycles(&pitched.graph, &m).expect("pitched complete price")
            <= ktpu_graph::cycles(&explicit.graph, &m).expect("materialized complete price")
    );
    let mut next_output = output_boundary.clone();
    next_output["args"]["dst_first"] = json!(20);
    next_output["args"]["rows"] = json!(11);
    let mut next_input = input_boundary.clone();
    next_input["args"]["src_first"] = json!(0);
    let next_explicit = ktpu_kernels::gen::chain::kernel(
        emitter(&product),
        emitter(&next_output),
        "src",
        "out",
        false,
    )
    .expect("next explicit output prefix");
    let next_explicit =
        ktpu_kernels::gen::chain::kernel(emitter(&next_input), next_explicit, "a", "dst", false)
            .expect("next explicit input prefix");
    let explicit_sequence =
        ktpu_kernels::gen::chain::sequence::kernel(vec![explicit, next_explicit])
            .expect("explicit two-chunk schedule");
    let mut next_descriptor = pitched_descriptor.clone();
    next_descriptor["first"]["args"]["src_first"] = json!(0);
    next_descriptor["second"]["args"]["second"] = next_output;
    let sequence_descriptor = json!({"members": [
        {"gen": "kernel.chain", "args": pitched_descriptor},
        {"gen": "kernel.chain", "args": next_descriptor}
    ]});
    let sequence = ktpu_kernels::call("kernel.sequence", &sequence_descriptor, &m)
        .expect("typed two-chunk schedule");
    assert!(
        ktpu_graph::cycles(&sequence.graph, &m).expect("complete typed sequence price")
            <= ktpu_graph::cycles(&explicit_sequence.graph, &m).expect("complete reference price")
    );
    let (expected, _) = simulate(&explicit_sequence);
    let (actual, writes) = simulate(&sequence);
    assert_eq!(actual, expected);
    for (byte, count) in writes.iter().enumerate() {
        let row = byte / 64;
        assert_eq!(*count, u32::from((7..31).contains(&row)));
    }
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("pitched owners");
    req.force = Some("rows".into());
    let distributed = call_split("kernel.sequence", &sequence_descriptor, &req, &m)
        .expect("dependent pitched row stages");
    assert_eq!(distributed.split.active_nodes, 4);
    validate_ownership(&distributed.split.output).expect("pitched output owners");
    assert_eq!(
        distributed.split.cost.makespan,
        ktpu_graph::cycles(&distributed.kernel.graph, &m).expect("complete emitted cost")
    );
    assert_eq!(distributed.split.cost.local_cycles.len(), 4);
    let mut linked_reads = BTreeMap::new();
    for scratch in &distributed.kernel.graph.scratch {
        if scratch.name.ends_with("_dst") && scratch.spec.shape == [16, 64] {
            linked_reads.insert(scratch.name.clone(), vec![false; 16 * 64]);
        }
    }
    assert!(!linked_reads.is_empty(), "pitched links are retained");
    for share in &distributed.split.shards {
        for region in &share.read_regions {
            if let Some(covered) = linked_reads.get_mut(&region.allocation) {
                assert!(region.bytes.start % 128 < 64);
                assert!(region.bytes.end <= (region.bytes.start / 128) * 128 + 64);
                for byte in region.bytes.clone() {
                    let row = (byte / 128) as usize;
                    assert!(row < 16);
                    covered[row * 64 + (byte % 128) as usize] = true;
                }
            }
        }
    }
    for (allocation, covered) in linked_reads {
        assert!(
            covered.iter().all(|read| *read),
            "missing linked input bytes in {allocation}"
        );
    }
    let (actual, writes) = simulate(&distributed.kernel);
    assert_eq!(actual, expected);
    for (byte, count) in writes.iter().enumerate() {
        assert_eq!(*count, u32::from((7..31).contains(&(byte / 64))));
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
#[path = "batched_matmul.rs"]
mod batched_matmul;
#[path = "bounded_matmul.rs"]
mod bounded_matmul;
#[path = "joint_matmul.rs"]
mod joint_matmul;
#[path = "loop_ownership.rs"]
mod loop_ownership;
#[path = "owner_tiles.rs"]
mod owner_tiles;
#[path = "packed_matmul.rs"]
mod packed_matmul;
#[path = "prepared_split.rs"]
mod prepared_split;
#[path = "split_cost.rs"]
mod split_cost;
#[path = "staging.rs"]
mod staging;
#[path = "wide_fold_split.rs"]
mod wide_fold_split;

fn simulate_matmul(kernel: &ktpu_kernels::kernel::Kernel) -> (Vec<u8>, Vec<u32>) {
    simulate_matmul_inputs(
        kernel,
        &|side, group, block, lane, k| {
            let tag = group * 71 + block * 29 + lane * 13 + k * 3 + i64::from(side);
            0x2400 | (tag as u16 & 0x3ff) | (((tag / 5) as u16 & 1) << 15)
        },
        &|i| 0x2800u16 | ((i * 37) as u16 & 0x3ff),
    )
}

fn simulate_matmul_inputs(
    kernel: &ktpu_kernels::kernel::Kernel,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
) -> (Vec<u8>, Vec<u32>) {
    simulate_named_inputs(kernel, "out", input, extra)
}

fn simulate_named_inputs(
    kernel: &ktpu_kernels::kernel::Kernel,
    output: &str,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
) -> (Vec<u8>, Vec<u32>) {
    simulate_named_tables(kernel, output, input, extra, &[])
}

fn simulate_named_tables(
    kernel: &ktpu_kernels::kernel::Kernel,
    output: &str,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
    tables: &[(&str, Vec<u8>)],
) -> (Vec<u8>, Vec<u32>) {
    simulate_named_executable(kernel, None, output, input, extra, tables, &machine())
}

fn simulate_matmul_executable(
    kernel: &ktpu_kernels::kernel::Kernel,
    l1: Option<&str>,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
    card: &Machine,
) -> (Vec<u8>, Vec<u32>) {
    simulate_named_executable(kernel, l1, "out", input, extra, &[], card)
}

fn simulate_named_executable(
    kernel: &ktpu_kernels::kernel::Kernel,
    l1: Option<&str>,
    output: &str,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
    tables: &[(&str, Vec<u8>)],
    card: &Machine,
) -> (Vec<u8>, Vec<u32>) {
    let text = ktpu_graph::lower(&kernel.graph).expect("L2 graph");
    let module = ktpu_l1::text::read(&text.text(), "matmul shares").expect("L2 text");
    let executable =
        l1.map(|source| ktpu_l1::text::read(source, "matmul delivery").expect("L1 motif"));
    let body = match &executable {
        Some(module) => module.body(&text.name, "l1").expect("L1 body"),
        None => module.body(&text.name, "l2").expect("L2 body"),
    };
    let mut binds = HashMap::new();
    let mut buffers = Vec::new();
    let mut end = 4096;
    for (name, ty) in &body.params {
        let b = SimBuf::of(ty, end).expect("physical buffer");
        binds.insert(name.clone(), end);
        end += (b.bytes() + 4095) / 4096 * 4096;
        buffers.push((name.clone(), b));
    }
    let mut proof_memory = ktpu_verify::MemoryContext::default();
    for (name, bytes) in tables {
        proof_memory
            .initial
            .insert(binds[*name] as u64, bytes.clone());
    }
    let proof_binds = binds
        .iter()
        .map(|(name, &at)| (name.clone(), i128::from(at)))
        .collect();
    match l1 {
        Some(source) => {
            common::verify_card_l1(source, &text.name, card, proof_binds, &proof_memory)
        }
        None => common::verify_card_l2(&text.text(), &text.name, card, proof_binds, &proof_memory),
    }
    let mut mem = Dram::new(end as usize);
    for (name, b) in &buffers {
        if l1.is_some()
            && !kernel
                .graph
                .ports
                .iter()
                .any(|port| port.name == *name && port.role == ktpu_graph::Role::In)
        {
            continue;
        }
        if let Some((_, bytes)) = tables.iter().find(|(table, _)| *table == name.as_str()) {
            mem.write(b.at, bytes).expect("table input");
        } else if let Layout::Mx7 { b: side, .. } = b.layout {
            for group in 0..b.shape[0] / 4 {
                for block in 0..b.shape[1] / 32 {
                    let mut values = [[0u16; 32]; 4];
                    for (lane, row) in values.iter_mut().enumerate() {
                        for (k, value) in row.iter_mut().enumerate() {
                            *value = input(side, group, block, lane as i64, k as i64);
                        }
                    }
                    let at = b.entry(group, block).expect("packed address");
                    for (word, bytes) in mx7::entry(&values).write(side).iter().enumerate() {
                        mem.set_word(at + word as i64 * 32, bytes)
                            .expect("packed input");
                    }
                }
            }
        } else if name == "mask" {
            let masks = ktpu_kernels::gen::transpose::masks(&machine()).expect("transpose masks");
            for (i, code) in masks.iter().enumerate() {
                mem.write(b.at + 2 * i as i64, &code.to_le_bytes())
                    .expect("transpose mask input");
            }
        } else if name != output {
            for i in 0..b.bytes() / 2 {
                let code = extra(i);
                mem.write(b.at + 2 * i, &code.to_le_bytes())
                    .expect("epilogue input");
            }
        }
    }
    let (_, out) = buffers.iter().find(|(n, _)| n == output).expect("output");
    let start = out.at as usize;
    mem.watch_writes(start..start + out.bytes() as usize)
        .expect("watch the output");
    if let Some(executable) = &executable {
        let facts = Facts::of(card);
        let mut simulator = ktpu_sim::machine::Machine::of_meshes(
            mem,
            card.nodes() as usize,
            facts.vc as usize,
            facts.mg as usize,
            card.vc.regs as usize,
            7,
        );
        for (kind, vc) in [
            (ktpu_machine::UnitKind::Vc, true),
            (ktpu_machine::UnitKind::Mg, false),
        ] {
            for (index, coord) in card.units(kind).iter().enumerate() {
                simulator.places.insert(
                    (coord.x(), coord.y()),
                    if vc {
                        ktpu_sim::units::link::Place::Vc(index)
                    } else {
                        ktpu_sim::units::link::Place::Mg(index)
                    },
                );
            }
        }
        ktpu_sim::l1::run(executable, &text.name, &binds, &mut simulator)
            .expect("simulated delivery motif");
        mem = simulator.mem;
    } else {
        ktpu_sim::l2::run(&module, &text.name, &binds, &mut mem, 7).expect("simulated shares");
    }
    let bytes = mem.read(out.at, out.bytes() as usize).expect("C").to_vec();
    (bytes, mem.write_counts().expect("write census").to_vec())
}

#[test]
fn matmul_output_splits_preserve_k_order_and_write_each_word_once() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 69, "n": 192, "k": 64,
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
            let valid = 69 * 192 * 2;
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
fn matmul_b_pass_preserves_k_order_and_writes_each_word_once() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 76, "n": 64, "k": 256
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 64, "rows": 76},
        "ring_depth": 2, "tail_bands": true
    });
    let input = |side, group, block, lane, k| {
        let tag = group * 71 + block * 29 + lane * 13 + k * 3 + i64::from(side);
        0x2400 | (tag as u16 & 0x3ff) | (((tag / 5) as u16 & 1) << 15)
    };
    let extra = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("P=1");
    let (single, _) = simulate_matmul_inputs(&baseline, &input, &extra);
    let single = decoded(&single);
    let value = |side, row: i64, k: i64| {
        ktpu_sim::num::value(ktpu_sim::num::from_f16(input(
            side,
            row / 4,
            k / 32,
            row % 4,
            k % 32,
        ))) as f32
    };
    let mut reference = Vec::new();
    for row in 0..76 {
        for col in 0..64 {
            reference.push(
                (0..256)
                    .map(|k| value(false, row, k) * value(true, col, k))
                    .sum::<f32>()
                    + ktpu_sim::num::value(ktpu_sim::num::from_f16(extra(col))) as f32,
            );
        }
    }
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let p = grant.len();
        let mut req = request(&m);
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        req.force = Some("m".into());
        let plain = call_split("matmul.mm_kernel", &args, &req, &m).expect("M split");
        let (want, _) = simulate_matmul_inputs(&plain.kernel, &input, &extra);
        let ids = if p == 4 {
            vec!["b-pass", "b-pass-relay", "b-pass-2x2", "b-pass-2x2-relay"]
        } else {
            vec!["b-pass", "b-pass-relay"]
        };
        for id in ids {
            req.force = Some(id.into());
            let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("B delivery");
            let chosen = ktpu_kernels::split::cost::select_split(
                vec![plain.split.clone(), plan.split.clone()],
                None,
            )
            .expect("priced alternatives");
            assert_eq!(chosen.id, "m", "raw delivery can lose for a short M share");
            let executable = plan.l1.as_deref().expect("raw peer delivery executable");
            let (actual, writes) =
                simulate_matmul_executable(&plan.kernel, Some(executable), &input, &extra, &m);
            let values = decoded(&actual);
            println!(
                "{id} P={p}: against P=1: {}; against f32: {}; P=1 against f32: {}",
                error_statistics_columns(&values, &single, 64),
                error_statistics_columns(&values, &reference, 64),
                error_statistics_columns(&single, &reference, 64),
            );
            assert_eq!(actual, want, "{id} at P={p} against M split");
            for (word, counts) in writes.chunks(32).enumerate() {
                assert!(
                    counts.iter().all(|&n| n == 1),
                    "{id} P={p}, word {word}: {counts:?}"
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
            "bt": 2, "m": 37, "n": 160, "k": 64, "shared": false,
            "n_tile": "auto", "waves": true
        }}},
        "shape_class": {"kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"},
        "e": {"nodes": [{"load": 0}], "root": 0},
        "extras": [],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 160, "rows": 80}
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

fn k_input(side: bool, group: i64, block: i64, lane: i64, k: i64) -> u16 {
    if side && group == 0 && lane == 0 {
        return 0;
    }
    let tag = group * 71 + block * 29 + lane * 13 + k * 3 + i64::from(side);
    0x2400 | (tag as u16 & 0x3ff) | (((tag / 5) as u16 & 1) << 15)
}

fn decoded(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(2)
        .map(|x| {
            ktpu_sim::num::value(ktpu_sim::num::from_f16(u16::from_le_bytes([x[0], x[1]]))) as f32
        })
        .collect()
}

fn error_statistics(actual: &[f32], reference: &[f32]) -> String {
    error_statistics_columns(actual, reference, 32)
}

fn error_statistics_columns(actual: &[f32], reference: &[f32], columns: usize) -> String {
    let mut errors: Vec<_> = actual
        .iter()
        .zip(reference)
        .map(|(a, b)| (a - b).abs())
        .collect();
    let nonfinite = actual.iter().filter(|x| !x.is_finite()).count();
    let worst = errors
        .iter()
        .enumerate()
        .max_by(|(_, a), (_, b)| a.total_cmp(b))
        .map(|(i, _)| i)
        .expect("nonempty result");
    let mut relative: Vec<_> = actual
        .iter()
        .zip(reference)
        .filter(|(_, b)| **b != 0.0)
        .map(|(a, b)| ((a - b) / b).abs())
        .collect();
    relative.sort_by(f32::total_cmp);
    let rel_max = relative.last().copied().unwrap_or(0.0);
    let rel_mean = relative.iter().sum::<f32>() / relative.len().max(1) as f32;
    let rel_median = if relative.is_empty() {
        0.0
    } else {
        (relative[(relative.len() - 1) / 2] + relative[relative.len() / 2]) / 2.0
    };
    let zero = actual
        .iter()
        .zip(reference)
        .filter(|(_, b)| **b == 0.0)
        .map(|(a, _)| a.abs())
        .fold(0.0f32, f32::max);
    let mean = errors.iter().sum::<f32>() / errors.len() as f32;
    let rms = (errors.iter().map(|x| x * x).sum::<f32>() / errors.len() as f32).sqrt();
    errors.sort_by(f32::total_cmp);
    format!(
        "abs_max={} mean={mean} median={} p95={} rms={rms} rel_max={rel_max} rel_mean={rel_mean} rel_median={rel_median} zero_ref_abs={zero} nonfinite={nonfinite} worst=({}, {})",
        errors[errors.len() - 1],
        (errors[(errors.len() - 1) / 2] + errors[errors.len() / 2]) / 2.0,
        errors[errors.len() * 95 / 100],
        worst / columns,
        worst % columns,
    )
}

#[test]
fn matmul_k_reductions_apply_bias_once_and_write_each_word_once() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 1, "m": 32, "n": 32, "k": 512
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 32},
        "ring_depth": 2, "tail_bands": true
    });
    let run = |kernel: &ktpu_kernels::kernel::Kernel| {
        simulate_matmul_inputs(kernel, &k_input, &|_| 0x3000)
    };
    let baseline = ktpu_kernels::call("matmul.mm_kernel", &args, &m).expect("P=1");
    let (want, _) = run(&baseline);
    let want = decoded(&want);
    let value = |side, row: i64, k: i64| {
        ktpu_sim::num::value(ktpu_sim::num::from_f16(k_input(
            side,
            row / 4,
            k / 32,
            row % 4,
            k % 32,
        ))) as f32
    };
    let mut reference = Vec::new();
    for row in 0..32 {
        for col in 0..32 {
            reference.push(
                (0..512)
                    .map(|k| value(false, row, k) * value(true, col, k))
                    .sum::<f32>()
                    + 0.125,
            );
        }
    }
    let baseline_stats = error_statistics(&want, &reference);
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let mut req = request(&m);
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        for id in ["k-tree", "k-serial"] {
            req.force = Some(id.into());
            let plan = call_split("matmul.mm_kernel", &args, &req, &m).expect("K reduction");
            let (actual, writes) = run(&plan.kernel);
            let actual = decoded(&actual);
            let stats = format!(
                "{id} P={}: against P=1: {}; against f32: {}; P=1 against f32: {}",
                req.nodes.ids().len(),
                error_statistics(&actual, &want),
                error_statistics(&actual, &reference),
                baseline_stats,
            );
            // FP22 reduction order differs from P=1: the statistics are reported, not gated.
            assert!(actual.iter().all(|a| a.is_finite()), "{stats}");
            assert!(
                (0..32).all(|row| actual[row * 32] == 0.125),
                "bias-only column: {stats}"
            );
            for (word, counts) in writes.chunks(32).enumerate() {
                assert!(
                    counts.iter().all(|&n| n == 1),
                    "word {word}: {counts:?}; {stats}"
                );
            }
        }
    }
}

#[test]
fn row_norm_and_softmax_shares_preserve_each_row_and_write_once() {
    let m = machine();
    let norm = json!({"nodes": [
        {"load": 0}, {"reduce": "sum", "arg": 0}, {"const": 0.03125},
        {"op": "mul", "args": [1, 2]}, {"op": "sub", "args": [0, 3]},
        {"op": "mul", "args": [4, 4]}, {"reduce": "sum", "arg": 5},
        {"op": "mul", "args": [6, 2]}, {"const": 0.00001},
        {"op": "add", "args": [7, 8]}, {"op": "rsqrt", "args": [9]},
        {"op": "mul", "args": [4, 10]}, {"load": 1},
        {"op": "mul", "args": [11, 12]}, {"load": 2},
        {"op": "add", "args": [13, 14]}
    ], "root": 15});
    let softmax = json!({"nodes": [
        {"load": 0}, {"reduce": "max", "arg": 0},
        {"op": "sub", "args": [0, 1]}, {"const": 1.4426950408889634},
        {"op": "mul", "args": [2, 3]}, {"op": "exp2", "args": [4]},
        {"reduce": "sum", "arg": 5}, {"op": "div", "args": [5, 6]}
    ], "root": 7});
    for e in [norm, softmax] {
        let args = json!({
            "R": 69, "W": 32, "e": e,
            "operands": [
                {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 69},
                {"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0},
                {"kind": "scalar", "dtype": "f16", "offset": 3, "pitch": 0, "rows": 0}
            ],
            "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 69}
        });
        let baseline = ktpu_kernels::call("vector.rows_kernel", &args, &m).expect("P=1");
        let (want, _) = simulate_matmul(&baseline);
        for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
            let mut req = request(&m);
            req.nodes = NodeSet::new(grant, &m).expect("grant");
            req.force = Some("rows".into());
            let got = call_split("vector.rows_kernel", &args, &req, &m).expect("row shares");
            assert_eq!(got.split.active_nodes, req.nodes.ids().len());
            validate_ownership(&got.split.output).expect("line owners");
            let (actual, writes) = simulate_matmul(&got.kernel);
            assert_eq!(actual, want);
            assert!(writes.iter().all(|&n| n == 1), "one output write");
        }
    }
}

#[test]
fn class_statistics_store_at_global_origin_including_the_tail() {
    let m = machine();
    let mut args = json!({
        "R": 197, "W": 32,
        "e": {"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1},
        "operands": [{"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 197}],
        "out": {"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0},
        "shape_class": {"kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"}
    });
    let baseline = ktpu_kernels::call("vector.rows_kernel", &args, &m).expect("origin zero");
    let (want, _) = simulate_matmul(&baseline);
    args["out"]["offset"] = json!(32);
    let shifted = ktpu_kernels::call("vector.rows_kernel", &args, &m).expect("global origin");
    let (actual, writes) = simulate_matmul(&shifted);
    assert_eq!(&actual[64..64 + 394], &want[..394]);
    assert!(writes[..64].iter().all(|&n| n == 0));
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let mut req = request(&m);
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        req.force = Some("rows".into());
        let got = call_split("vector.rows_kernel", &args, &req, &m).expect("class row shares");
        validate_ownership(&got.split.output).expect("statistic lines");
        let (actual, writes) = simulate_matmul(&got.kernel);
        assert_eq!(&actual[64..64 + 394], &want[..394]);
        assert!(writes[..64].iter().all(|&n| n == 0));
        assert!(writes[64..].iter().all(|&n| n == 1));
    }
}

#[test]
fn flat_shares_keep_scalar_origins_and_exact_final_words() {
    let m = machine();
    let args = json!({
        "n": 197 * 32 - 16, "dtypes": ["f16", "f16"], "scalars": [[1, 3]],
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2}
    });
    let baseline = ktpu_kernels::call("vector.flat_kernel", &args, &m).expect("flat P=1");
    let (want, _) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("tokens".into());
    let got = call_split("vector.flat_kernel", &args, &req, &m).expect("flat shares");
    validate_ownership(&got.split.output).expect("flat word owners");
    let (actual, writes) = simulate_matmul(&got.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn row_empty_shares_have_no_instances_or_writes() {
    let m = machine();
    let args = json!({
        "R": 1, "W": 32,
        "e": {"nodes": [{"load": 0}], "root": 0},
        "operands": [{"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 1}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 1}
    });
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let got = call_split("vector.rows_kernel", &args, &req, &m).expect("one row");
    let program = ktpu_graph::program(&got.kernel.graph, &m).expect("graph");
    let body = program.body().expect("body");
    assert!(ktpu_l2::ir::instances(&body)
        .expect("instances")
        .iter()
        .all(|s| s.node == 3));
    let baseline = ktpu_kernels::call("vector.rows_kernel", &args, &m).expect("one row P=1");
    let (want, _) = simulate_matmul(&baseline);
    let (actual, writes) = simulate_matmul(&got.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn row_composites_keep_statistics_and_broadcasts_at_global_origins() {
    let m = machine();
    let full = json!({"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 197});
    let stat = json!({"kind": "row", "dtype": "f16", "offset": 32, "pitch": 0, "rows": 240});
    let col = json!({"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0});
    let reduce = json!({"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1});
    let add = json!({"nodes": [{"load": 0}, {"load": 1}, {"load": 2},
        {"op": "add", "args": [0, 1]}, {"op": "add", "args": [3, 2]}], "root": 4});
    let chain = json!({
        "first": {"gen": "vector.rows_kernel", "args": {
            "R": 197, "W": 32, "e": reduce, "operands": [full], "out": stat
        }},
        "second": {"gen": "vector.rows_kernel", "args": {
            "R": 197, "W": 32, "e": add, "operands": [stat, full, col], "out": full
        }}, "from": "in0", "to": "out", "keep": false
    });
    let fused = json!({
        "ports": [
            {"name": "x", "shape": [197, 32], "dtype": "f16", "written": false},
            {"name": "y", "shape": [197, 32], "dtype": "f16", "written": false},
            {"name": "bias", "shape": [32], "dtype": "f16", "written": false},
            {"name": "out", "shape": [197, 32], "dtype": "f16", "written": true}
        ],
        "scratch": [{"name": "stat", "shape": [240], "dtype": "f16"}], "staged": [],
        "steps": [
            {"kind": "rows", "R": 197, "W": 32, "exprs": [reduce],
                "operands": [full], "ins": ["x"], "out_operands": [stat], "outs": ["stat"]},
            {"kind": "rows", "R": 197, "W": 32, "exprs": [add],
                "operands": [stat, full, col], "ins": ["stat", "y", "bias"],
                "out_operands": [full], "outs": ["out"]}
        ]
    });
    for (gen, args, output) in [
        ("kernel.chain", chain, "c2_out"),
        ("fused.kernel", fused, "out"),
    ] {
        let simulate = |k: &ktpu_kernels::kernel::Kernel| {
            simulate_named_inputs(k, output, &|_, _, _, _, _| 0, &|i| {
                0x2800 | ((i * 37) as u16 & 0x3ff)
            })
        };
        let baseline = ktpu_kernels::call(gen, &args, &m).expect("local composite");
        let (want, _) = simulate(&baseline);
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
        req.force = Some("rows".into());
        let got = call_split(gen, &args, &req, &m).expect("composite shares");
        validate_ownership(&got.split.output).expect("composite owners");
        let (actual, writes) = simulate(&got.kernel);
        assert_eq!(actual, want, "{gen}");
        assert!(writes.iter().all(|&n| n == 1), "{gen}");
    }
}

#[test]
fn fused_complete_fold_keeps_odd_partials_and_global_row_origins() {
    let m = machine();
    let full = json!({"kind": "full", "dtype": "f16", "offset": 0, "pitch": 384, "rows": 128});
    let stat = json!({"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 128});
    let mean = json!({"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0},
        {"const": 0.0026041666666666665}, {"op": "mul", "args": [1, 2]}], "root": 3});
    let mut reference = ktpu_kernels::call(
        "reductions.wide_fold_kernel",
        &json!({"R": 128, "W": 384, "e": mean, "operands": [full], "out": stat}),
        &m,
    )
    .expect("complete reference fold");
    for scratch in &mut reference.graph.scratch {
        scratch.spec.space = ktpu_graph::BufSpace::Stage;
    }
    let args = json!({
        "ports": [
            {"name": "in0", "shape": [128, 384], "dtype": "f16", "written": false},
            {"name": "out", "shape": [128], "dtype": "f16", "written": true}
        ],
        "scratch": [], "staged": [], "scratch_space": "stage",
        "steps": [{"kind": "rows", "fold": true, "R": 128, "W": 384,
            "exprs": [mean], "operands": [full], "ins": ["in0"],
            "out_operands": [stat], "outs": ["out"]}]
    });
    let simulate = |k: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(k, "out", &|_, _, _, _, _| 0, &|i| {
            0x2000 | ((i / 384) as u16 * 16) | (i as u16 & 15)
        })
    };
    let (want, _) = simulate(&reference);
    let local = ktpu_kernels::call("fused.kernel", &args, &m).expect("fused fold");
    let (actual, writes) = simulate(&local);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
    assert_eq!(
        ktpu_graph::cycles(&local.graph, &m).expect("fused schedule"),
        ktpu_graph::cycles(&reference.graph, &m).expect("reference schedule")
    );
    // A complete fold's singleton phases rotate without changing its work.
    let mut balanced_args = args.clone();
    balanced_args["steps"][0]["R"] = json!(16);
    balanced_args["scratch_space"] = json!("mem");
    let mut placed =
        ktpu_kernels::call("fused.kernel", &balanced_args, &m).expect("small complete fold");
    let mut singleton = 0;
    for stmt in &mut placed.graph.body {
        if let ktpu_graph::Node::Par { lo, hi, unit, .. } = &mut stmt.node {
            if unit.kind == ktpu_graph::UnitKind::Vc
                && *lo == ktpu_graph::Index::Int(0)
                && *hi == ktpu_graph::Index::Int(1)
            {
                unit.index = Some(ktpu_graph::Index::Int(singleton % Facts::of(&m).vc));
                singleton += 1;
            }
        }
    }
    assert!(
        singleton >= 2,
        "the complete fold exercises successive singleton phases"
    );
    balanced_args["balance_stages"] = json!(true);
    let balanced =
        ktpu_kernels::call("fused.kernel", &balanced_args, &m).expect("balanced complete fold");
    assert_eq!(balanced.graph.body, placed.graph.body);
    assert_eq!(
        ktpu_graph::cycles(&balanced.graph, &m).expect("balanced price"),
        ktpu_graph::cycles(&placed.graph, &m).expect("explicit placement price")
    );
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let split = call_split("fused.kernel", &args, &req, &m).expect("fold row owners");
    assert_eq!(split.split.active_nodes, 4);
    validate_ownership(&split.split.output).expect("one writer per statistic line");
    let (actual, writes) = simulate(&split.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn product_chains_keep_quant_and_ring_work_on_each_row_owner() {
    let m = machine();
    let product = |width, columns| {
        json!({
            "gen": "matmul.mm_kernel", "args": {
                "mm": {"made_by": {"gen": "matmul.plan", "args": {
                    "bt": 1, "m": 64, "n": columns, "k": width,
                    "pad_m": false, "dense_n": true
                }}},
                "activation_any": true, "ring_depth": 2,
                "quant_backend": "vector_mx7",
                "e": {"nodes": [{"load": 0}, {"load": 1},
                    {"op": "add", "args": [0, 1]}], "root": 2},
                "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
                "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": columns, "rows": 64}
            }
        })
    };
    // A single row tile and many column waves share packed activation
    // capacity. Only the completed middle columns may quantize early.
    let mut window_args = json!({
        "first": product(64, 512), "second": product(512, 32),
        "from": "a", "to": "out", "keep": false, "link_space": "stage"
    });
    for side in ["first", "second"] {
        window_args[side]["args"]["quant_backend"] = json!("mover_quant");
        window_args[side]["args"]["ring_depth"] = json!(1);
        window_args[side]["args"]["mm"]["made_by"]["args"]["m"] = json!(16);
        window_args[side]["args"]["out"]["rows"] = json!(16);
    }
    window_args["first"]["args"]["mm"]["made_by"]["args"]["n_tiles"] = json!(16);
    window_args["second"]["args"]["mm"]["made_by"]["args"]["n_tiles"] = json!(1);
    let unsplit_windows =
        ktpu_kernels::call("kernel.chain", &window_args, &m).expect("shared complete quant stage");
    // A real product's final pipeline iteration still consumes its packed
    // activation. Repacking the next input into that same allocation must not
    // cross the iteration, even if a positional native rewrite did so.
    let input_quant = unsplit_windows
        .graph
        .body
        .iter()
        .find(|stmt| {
            matches!(
                stmt.node,
                ktpu_graph::Node::Mover {
                    op: ktpu_graph::MoverOp::Quant,
                    ..
                }
            )
        })
        .expect("activation packing")
        .clone();
    let product_wave = unsplit_windows
        .graph
        .body
        .iter()
        .find(|stmt| matches!(stmt.node, ktpu_graph::Node::For { pipe: Some(_), .. }))
        .expect("product pipeline");
    assert!(
        !ktpu_kernels::gen::chain::independent_tail(
            product_wave,
            std::slice::from_ref(&input_quant),
        ),
        "next activation cannot overwrite the final wave's packed input"
    );
    let mut private_quant = input_quant.clone();
    if let ktpu_graph::Node::Mover { dst, .. } = &mut private_quant.node {
        dst.buffer = "independent_next_activation".into();
    }
    assert!(
        ktpu_kernels::gen::chain::independent_tail(
            product_wave,
            std::slice::from_ref(&private_quant),
        ),
        "a truly independent activation buffer is eligible"
    );
    // Read/read sharing is legal: repack from an allocation the tail only
    // reads, into unrelated storage. Treating all touches as writes loses it.
    if let (ktpu_graph::Node::Mover { dst, .. }, ktpu_graph::Node::Mover { src: Some(src), .. }) =
        (&input_quant.node, &mut private_quant.node)
    {
        src.buffer = dst.buffer.clone();
    }
    assert!(
        ktpu_kernels::gen::chain::independent_tail(
            product_wave,
            std::slice::from_ref(&private_quant),
        ),
        "shared immutable reads do not create a dependency"
    );
    // The same effect proof drives ordinary fast attention transfers. A real
    // computation blocks aliased repacking, but disjoint transfers move ahead.
    let no_outputs = std::collections::HashSet::new();
    let mut blocked = vec![product_wave.clone(), input_quant.clone()];
    assert!(!ktpu_kernels::gen::chain::hoist_inputs(
        &mut blocked,
        &no_outputs
    ));
    // Unlike the tail-only probe above, use a source that no iteration can
    // write: whole-wave motion must not infer immutability from its final trip.
    let mut immutable_quant = private_quant.clone();
    if let ktpu_graph::Node::Mover { src: Some(src), .. } = &mut immutable_quant.node {
        src.buffer = "immutable_next_source".into();
    }
    let mut independent = vec![product_wave.clone(), immutable_quant.clone()];
    assert!(ktpu_kernels::gen::chain::hoist_inputs(
        &mut independent,
        &no_outputs
    ));
    assert_eq!(independent[0], immutable_quant);
    let mut public = vec![product_wave.clone(), immutable_quant.clone()];
    let ktpu_graph::Node::Mover { dst, .. } = &private_quant.node else {
        unreachable!()
    };
    assert!(!ktpu_kernels::gen::chain::hoist_inputs(
        &mut public,
        &std::collections::HashSet::from([dst.buffer.clone()]),
    ));
    let mut ready = vec![immutable_quant.clone(), product_wave.clone()];
    assert!(ktpu_kernels::gen::chain::advance_compute(
        &mut ready,
        &no_outputs
    ));
    assert_eq!(ready, vec![product_wave.clone(), immutable_quant.clone()]);
    let mut needs_input = vec![input_quant.clone(), product_wave.clone()];
    assert!(!ktpu_kernels::gen::chain::advance_compute(
        &mut needs_input,
        &no_outputs
    ));
    let mut public = vec![immutable_quant.clone(), product_wave.clone()];
    assert!(!ktpu_kernels::gen::chain::advance_compute(
        &mut public,
        &std::collections::HashSet::from([dst.buffer.clone()]),
    ));
    // Status records are writes too; they cannot alias the active input.
    if let (ktpu_graph::Node::Mover { dst, .. }, ktpu_graph::Node::Mover { kw, .. }) =
        (&input_quant.node, &mut private_quant.node)
    {
        kw.push(("status".into(), ktpu_graph::Attr::View(dst.clone())));
    }
    assert!(
        !ktpu_kernels::gen::chain::independent_tail(
            product_wave,
            std::slice::from_ref(&private_quant),
        ),
        "finite-status storage is a real write dependency"
    );
    window_args["pipeline_lookahead"] = json!(true);
    let windows = ktpu_kernels::call("kernel.chain", &window_args, &m)
        .expect("completed quant column windows");
    assert!(
        ktpu_graph::cycles(&windows.graph, &m).expect("window schedule price")
            <= ktpu_graph::cycles(&unsplit_windows.graph, &m).expect("unsplit schedule price")
    );
    let candidate = ktpu_kernels::gen::chain::pipeline_candidate(&unsplit_windows, &m)
        .expect("legal pooled window candidate");
    let candidate_price = ktpu_graph::cycles(&candidate.graph, &m).expect("window candidate price");
    let baseline_price = ktpu_graph::cycles(&unsplit_windows.graph, &m).expect("baseline price");
    let selected_price = ktpu_graph::cycles(&windows.graph, &m).expect("selected price");
    assert!(selected_price <= candidate_price.min(baseline_price),
        "all complete candidates compete: baseline={baseline_price}, windows={candidate_price}, selected={selected_price}");
    let quants = candidate
        .graph
        .body
        .iter()
        .filter(|stmt| {
            matches!(
                stmt.node,
                ktpu_graph::Node::Mover {
                    op: ktpu_graph::MoverOp::Quant,
                    ..
                }
            )
        })
        .count();
    assert_eq!(
        quants, 4,
        "one input quant and three complete packed column windows"
    );
    ktpu_graph::program(&candidate.graph, &m).expect("fresh window scopes");
    window_args["pipeline_lookahead"] = json!(false);
    let mut checked_args = json!({
        "members": [{"gen": "kernel.chain", "args": window_args}],
        "quant_backend": "vector_mx7", "pipeline_lookahead": false
    });
    let checked_baseline = ktpu_kernels::call("kernel.sequence", &checked_args, &m)
        .expect("decorated complete baseline");
    checked_args["pipeline_lookahead"] = json!(true);
    let checked = ktpu_kernels::call("kernel.sequence", &checked_args, &m)
        .expect("status-safe quant-window sequence");
    assert!(
        ktpu_graph::cycles(&checked.graph, &m).expect("decorated selected price")
            <= ktpu_graph::cycles(&checked_baseline.graph, &m).expect("decorated baseline price")
    );
    let decorated_windows = ktpu_kernels::gen::chain::quant::kernel(
        candidate,
        ktpu_kernels::gen::chain::quant::Backend::Vector,
        &m,
    )
    .expect("decorate actual split windows");
    for (tested, split_windows) in [(&decorated_windows, true), (&checked, false)] {
        let mut records = Vec::new();
        for stmt in &tested.graph.body {
            if let ktpu_graph::Node::Mover {
                op: ktpu_graph::MoverOp::Quant,
                kw,
                ..
            } = &stmt.node
            {
                let record = kw
                    .iter()
                    .find_map(|(key, value)| match (key.as_str(), value) {
                        ("status", ktpu_graph::Attr::View(view)) => Some(view.clone()),
                        _ => None,
                    })
                    .expect("each scheduled quant owns a status slice");
                assert!(
                    !records.contains(&record),
                    "quant windows cannot share status records"
                );
                records.push(record);
            }
        }
        assert!(records.len() >= 2);
        if split_windows {
            assert_eq!(
                records.len(),
                4,
                "split windows each need their own finite record"
            );
        }
        assert_eq!(
            tested
                .graph
                .body
                .iter()
                .filter(|stmt| matches!(
                    stmt.node,
                    ktpu_graph::Node::Mover {
                        op: ktpu_graph::MoverOp::QuantCheck,
                        ..
                    }
                ))
                .count(),
            1
        );
        assert!(matches!(
            tested.graph.body.last().expect("final check").node,
            ktpu_graph::Node::Mover {
                op: ktpu_graph::MoverOp::QuantCheck,
                ..
            }
        ));
        ktpu_graph::program(&tested.graph, &m).expect("status slices and final check");
    }
    let mut args = json!({
        "first": product(64, 128), "second": product(128, 32),
        "from": "a", "to": "out", "keep": false
    });
    let simulate = |k: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(
            k,
            &k.graph
                .ports
                .iter()
                .find(|port| port.role == ktpu_graph::Role::Out)
                .expect("chain output")
                .name,
            &|_, group, block, lane, col| {
                0x2400 | ((group * 17 + block * 31 + lane * 7 + col) as u16 & 0x3ff)
            },
            &|i| 0x2400 + (i / 64) as u16 * 8 + (i as u16 & 7),
        )
    };
    let first =
        ktpu_kernels::call("matmul.mm_kernel", &args["first"]["args"], &m).expect("first product");
    let second = ktpu_kernels::call("matmul.mm_kernel", &args["second"]["args"], &m)
        .expect("second product");
    // Explicit straight-line schedules: each quant records finite status and
    // the product finishes before the status reduction.
    let reference = |part: &serde_json::Value| {
        let mut plain = part["args"].clone();
        plain
            .as_object_mut()
            .expect("arguments")
            .remove("quant_backend");
        let mut kernel =
            ktpu_kernels::call("matmul.mm_kernel", &plain, &m).expect("ordinary product");
        let mut graph = serde_json::to_value(&kernel.graph).expect("graph");
        let record = ktpu_machine::packing::quant_status_bytes(m.quant_cores()) / 2;
        let body = graph["body"].as_array_mut().expect("schedule");
        let mut records = 0;
        for stmt in body.iter_mut() {
            if stmt["kind"] != "mover" || stmt["op"] != "quant" {
                continue;
            }
            let kw = stmt["kw"].as_array_mut().expect("quant attributes");
            kw.retain(|pair| pair[0] != "backend" && pair[0] != "status");
            kw.push(json!(["backend", {"str": "vector_mx7"}]));
            kw.push(json!(["status", {"view": {
                "buffer": "reference_status",
                "axes": [{"run": [records * record, record]}]
            }}]));
            for (key, value) in [
                ("finite_batch", 4),
                ("overlap", 1),
                ("pipeline", 1),
                ("zero_classifier", 1),
                ("stream_axes", 1),
                ("fill_overlap", 1),
                ("zero_reuse", 1),
            ] {
                if !kw.iter().any(|pair| pair[0] == key) {
                    kw.push(json!([key, {"int": value}]));
                }
            }
            records += 1;
        }
        body.push(json!({
            "kind": "mover", "op": "quant_check",
            "dst": {"buffer": "reference_status", "axes": ["all"]},
            "src": null, "flags": [], "kw": [], "where": "<reference>"
        }));
        graph["scratch"]
            .as_array_mut()
            .expect("scratch")
            .push(json!({
                "name": "reference_status", "spec": {
                    "shape": [records * record], "dtype": "f16",
                    "layout": [], "space": "stage"
                }
            }));
        kernel.graph = serde_json::from_value(graph).expect("reference graph");
        kernel
    };
    let emitted =
        ktpu_kernels::gen::chain::kernel(first.clone(), second.clone(), "a", "out", false)
            .expect("separate stage allocations");
    let separate = ktpu_kernels::gen::chain::kernel(
        reference(&args["first"]),
        reference(&args["second"]),
        "a",
        "out",
        false,
    )
    .expect("explicit quant schedules");
    assert_eq!(
        ktpu_graph::cycles(&emitted.graph, &m).expect("typed quant schedule"),
        ktpu_graph::cycles(&separate.graph, &m).expect("explicit quant schedule"),
    );
    let full = json!({"kind": "full", "dtype": "f16", "offset": 0, "pitch": 128, "rows": 64});
    let activation = json!({
        "gen": "vector.rows_kernel", "args": {
            "R": 64, "W": 128, "operands": [full], "out": full,
            "e": {"nodes": [{"load": 0}, {"const": 0.50390625},
                {"op": "mul", "args": [0, 1]}], "root": 2}
        }
    });
    let activation_kernel = ktpu_kernels::call("vector.rows_kernel", &activation["args"], &m)
        .expect("materialized pointwise stage");
    let reference_first = ktpu_kernels::gen::chain::kernel(
        reference(&args["first"]),
        activation_kernel,
        "in0",
        "out",
        false,
    )
    .expect("explicit separate activation");
    let separate = ktpu_kernels::gen::chain::kernel(
        reference_first,
        reference(&args["second"]),
        "a",
        "c2_out",
        false,
    )
    .expect("explicit three-stage schedule");
    args["first"] = json!({"gen": "kernel.chain", "args": {
        "first": args["first"].clone(), "second": activation,
        "from": "in0", "to": "out", "keep": false,
        "materialized_pointwise": true
    }});
    args["to"] = json!("c2_out");
    let baseline = ktpu_kernels::call("kernel.chain", &args, &m).expect("local chain");
    fn rounded_stream(body: &[ktpu_graph::Stmt]) -> bool {
        use ktpu_graph::{Node, Operand, ValueOp};
        for (index, stmt) in body.iter().enumerate() {
            if let Node::Value {
                op: ValueOp::Unpack,
                target,
                args,
                ..
            } = &stmt.node
            {
                let [Operand::Value(packed)] = args.as_slice() else {
                    continue;
                };
                let stored_pack = body[..index]
                    .iter()
                    .any(|s| matches!(&s.node, Node::Store { src, .. } if src == packed))
                    && body[..index].iter().any(|s| {
                        matches!(&s.node, Node::Value { op: ValueOp::Pack, target, ty, .. }
                        if target == packed && ty.dtype == ktpu_graph::Dtype::F16)
                    });
                let consumer = body[index + 1..].iter().any(|s| {
                    matches!(&s.node, Node::Value { op: ValueOp::Mul, args, .. }
                        if args.contains(&Operand::Value(target.clone()))
                            && args.contains(&Operand::Float(ktpu_graph::Number::F(0.50390625))))
                });
                if stored_pack && consumer {
                    return true;
                }
            }
            if stmt.node.body().is_some_and(rounded_stream) {
                return true;
            }
        }
        false
    }
    assert!(
        baseline.graph.body.iter().any(|stmt| {
            matches!(&stmt.node, ktpu_graph::Node::For { pipe: Some(_), body, .. }
                if rounded_stream(body))
        }),
        "the pointwise consumer reads the stored FP16 pack inside the producer stream"
    );
    let stage_bytes = |k: &ktpu_kernels::kernel::Kernel| {
        k.graph
            .scratch
            .iter()
            .filter(|s| s.spec.space == ktpu_graph::BufSpace::Stage)
            .map(|s| {
                s.spec.shape.iter().product::<i64>() * ktpu_kernels::gen::Hw::item(s.spec.dtype)
            })
            .sum::<i64>()
    };
    assert!(stage_bytes(&baseline) < stage_bytes(&separate));
    assert!(
        ktpu_graph::cycles(&baseline.graph, &m).expect("shared schedule")
            <= ktpu_graph::cycles(&separate.graph, &m).expect("separate schedule")
    );
    let (want, _) = simulate(&separate);
    assert_eq!(simulate(&baseline).0, want);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![0], &m).expect("single owner");
    req.force = Some("row-local".into());
    let local = call_split("kernel.chain", &args, &req, &m).expect("local selection");
    assert_eq!(
        ktpu_graph::cycles(&local.kernel.graph, &m).expect("local schedule"),
        ktpu_graph::cycles(&baseline.graph, &m).expect("original schedule")
    );
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("row owners");
    let distributed = call_split("kernel.chain", &args, &req, &m).expect("local chains");
    assert_eq!(distributed.split.active_nodes, 4);
    validate_ownership(&distributed.split.output).expect("output owners");
    for share in &distributed.split.shards {
        for region in &share.scratch_regions {
            assert!(distributed
                .kernel
                .graph
                .scratch
                .iter()
                .any(|s| s.name == region.allocation));
        }
    }
    // A private FP16 edge and both products' rings stay off global DRAM.
    assert!(distributed
        .kernel
        .graph
        .scratch
        .iter()
        .all(|s| s.spec.space != ktpu_graph::BufSpace::Dram));
    let (actual, writes) = simulate(&distributed.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn row_chain_publishes_a_statistic_before_cross_row_broadcast() {
    let m = machine();
    let full = json!({"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 197});
    let args = json!({
        "first": {"gen": "vector.rows_kernel", "args": {
            "R": 197, "W": 32,
            "e": {"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1},
            "operands": [full],
            "out": {"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 208}
        }},
        "second": {"gen": "vector.rows_kernel", "args": {
            "R": 197, "W": 32,
            "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2},
            "operands": [
                {"kind": "scalar", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 208}, full
            ], "out": full
        }}, "from": "in0", "to": "out", "keep": false
    });
    let simulate = |k: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(k, "c2_out", &|_, _, _, _, _| 0, &|i| {
            0x2800 | ((i * 37) as u16 & 0x3ff)
        })
    };
    let baseline = ktpu_kernels::call("kernel.chain", &args, &m).expect("reference chain");
    let (want, _) = simulate(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let got = call_split("kernel.chain", &args, &req, &m).expect("distributed stages");
    assert_eq!(got.split.active_nodes, 4);
    validate_ownership(&got.split.output).expect("final owners");
    let (actual, writes) = simulate(&got.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
    for share in &got.split.shards {
        for region in &share.scratch_regions {
            assert!(got
                .kernel
                .graph
                .scratch
                .iter()
                .any(|s| s.name == region.allocation));
        }
    }
}

#[test]
fn staged_row_flat_package_keeps_shared_statistics_and_exact_copies() {
    let m = machine();
    let hw = ktpu_kernels::gen::Hw::of(&m);
    let total = (197 * 32 + hw.vlmax - 1) / hw.vlmax;
    let words = 197 * 32 * 2 / hw.word_bytes;
    let args = json!({
        "ports": [
            {"name": "x", "shape": [197, 32], "dtype": "f16", "written": false},
            {"name": "out", "shape": [197, 32], "dtype": "f16", "written": true}
        ],
        "scratch": [
            {"name": "stats", "shape": [208], "dtype": "f16"},
            {"name": "flat_x", "shape": [total, hw.vlmax], "dtype": "f16"},
            {"name": "flat_out", "shape": [total, hw.vlmax], "dtype": "f16"}
        ],
        "staged": [
            {"scratch": "flat_x", "port": "x", "words": words, "read": true, "written": false},
            {"scratch": "flat_out", "port": "out", "words": words, "read": false, "written": true}
        ],
        "steps": [
            {"kind": "rows", "R": 197, "W": 32,
                "exprs": [{"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1}],
                "operands": [{"kind": "full", "dtype": "f16", "offset": 0, "pitch": 32, "rows": 197}],
                "ins": ["x"],
                "out_operands": [{"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 208}],
                "outs": ["stats"]},
            {"kind": "flat", "total": total, "dtypes": ["f16", "f16"], "out_dtype": "f16",
                "exprs": [{"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}], "root": 2}],
                "ins": ["flat_x", "stats"], "lanes": [[1, 0]], "outs": ["flat_out"], "kept": [true]}
        ]
    });
    let baseline = ktpu_kernels::call("fused.kernel", &args, &m).expect("staged reference");
    let (want, _) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let got = call_split("fused.kernel", &args, &req, &m).expect("staged row/flat shares");
    assert_eq!(got.split.active_nodes, 4);
    validate_ownership(&got.split.output).expect("copy publishes exact output");
    let (actual, writes) = simulate_matmul(&got.kernel);
    assert_eq!(actual, want);
    assert!(writes.iter().all(|&n| n == 1));
}

#[test]
fn prepared_row_shares_read_their_rows_through_the_relayout() {
    // ViT's patch embedding: a transposed, padded conv output read composed
    // through a relayout, plus a bias column and a position table, written
    // one row below the class token.
    let m = machine();
    let (rows, width, padded) = (40, 64, 48);
    let args = json!({
        "R": rows, "W": width, "alias": {}, "composed": [0],
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]},
                        {"load": 2}, {"op": "add", "args": [2, 3]}], "root": 4},
        "made": {"0": [{"made_by": {"gen": "relayout.plan", "args": {
            "at": 1, "dat": 0, "dwalk": [[rows, width], [width, 1]], "keep": false,
            "m": rows * width, "src_n": width * padded, "tree": true,
            "walk": [[rows, 1], [width, padded]], "z0": 0}}}]},
        "operands": [
            {"dtype": "f16", "kind": "full", "offset": 0, "pitch": width, "rows": 0},
            {"dtype": "f16", "kind": "col", "offset": 0, "pitch": 0, "rows": 0},
            {"dtype": "f16", "kind": "full", "offset": 0, "pitch": width, "rows": 0}
        ],
        "out": {"dtype": "f16", "kind": "full", "offset": width, "pitch": width, "rows": 0}
    });
    let baseline = ktpu_kernels::call("prepared.rows_kernel", &args, &m).expect("one owner");
    let (want, once) = simulate_matmul(&baseline);
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
    req.force = Some("rows".into());
    let got = call_split("prepared.rows_kernel", &args, &req, &m).expect("row shares");
    assert_eq!(got.split.active_nodes, 4);
    validate_ownership(&got.split.output).expect("one writer a row");
    let (actual, writes) = simulate_matmul(&got.kernel);
    assert_eq!(
        actual, want,
        "each share reads its own rows of the relayout"
    );
    // The class-token row stays unwritten; every other word once, as one owner.
    assert_eq!(writes, once);
    assert!(writes.iter().all(|&n| n <= 1));
}

#[test]
fn groupnorm_population_merge_preserves_f32_states_and_empty_identity() {
    let m = machine();
    let lanes = m.vc.vlmax as usize;
    for (na, nb) in [(1, 3), (0, 4), (4, 0), (0, 0)] {
        let kernel = ktpu_kernels::call("groupnorm.merge_kernel", &json!({"na": na, "nb": nb}), &m)
            .expect("population merge");
        let text = ktpu_graph::lower(&kernel.graph).expect("merge graph");
        let module = ktpu_l1::text::read(&text.text(), "population merge").expect("L2 text");
        let body = module.body(&text.name, "l2").expect("body");
        let mut binds = HashMap::new();
        let mut buffers = HashMap::new();
        let mut end = 4096;
        for (name, ty) in &body.params {
            let buf = SimBuf::of(ty, end).expect("buffer");
            binds.insert(name.clone(), end);
            end += (buf.bytes() + 4095) / 4096 * 4096;
            buffers.insert(name.clone(), buf);
        }
        let mut mem = Dram::new(end as usize);
        let mut expected = Vec::new();
        for lane in 0..lanes {
            let base = 1.0 + lane as f32 / 32768.0;
            let population: Vec<_> = (0..na + nb).map(|i| base + i as f32 / 8.0).collect();
            let stats = |xs: &[f32]| {
                if xs.is_empty() {
                    return (f32::NAN, f32::NAN);
                }
                let mean = xs.iter().sum::<f32>() / xs.len() as f32;
                let m2 = xs.iter().map(|x| (x - mean) * (x - mean)).sum::<f32>();
                (mean, m2)
            };
            let (ma, m2a) = stats(&population[..na as usize]);
            let (mb, m2b) = stats(&population[na as usize..]);
            expected.push(if population.is_empty() {
                (0.0, 0.0)
            } else {
                stats(&population)
            });
            for (field, value) in [("mean_a", ma), ("m2_a", m2a), ("mean_b", mb), ("m2_b", m2b)] {
                mem.write(buffers[field].at + lane as i64 * 4, &value.to_le_bytes())
                    .expect("state input");
            }
        }
        let start = buffers["mean"].at.min(buffers["m2"].at) as usize;
        let stop = (buffers["mean"].at.max(buffers["m2"].at) + lanes as i64 * 4) as usize;
        mem.watch_writes(start..stop).expect("state writes");
        ktpu_sim::l2::run(&module, &text.name, &binds, &mut mem, 7).expect("merge execution");
        for (field, column) in [("mean", 0), ("m2", 1)] {
            let out = &buffers[field];
            let bytes = mem
                .read(out.at, out.bytes() as usize)
                .expect("merged state");
            for (lane, chunk) in bytes.chunks_exact(4).enumerate() {
                let want = if column == 0 {
                    expected[lane].0
                } else {
                    expected[lane].1
                };
                assert_eq!(chunk, want.to_le_bytes(), "{na}+{nb}, {field}, lane {lane}");
            }
            let lo = out.at as usize - start;
            let counts = mem.write_counts().expect("write census");
            assert!(counts[lo..lo + out.bytes() as usize]
                .iter()
                .all(|&n| n == 1));
        }
    }
}

#[test]
fn conv_patch_owners_preserve_halo_reads_and_group_slices() {
    let m = machine();
    let args = json!({"spec": {
        "batch": 1, "height": 3, "width": 5, "cin": 32, "cout": 64,
        "kh": 2, "kw": 3, "stride": [1, 2], "padding": [1, 2],
        "dilation": [2, 1], "groups": 2
    }});
    let tag = |i: i64| 0x3000 | (i as u16 & 0x3ff);
    let mut want = Vec::new();
    for group in 0..2 {
        for oh in 0..3 {
            for ow in 0..4 {
                for kh in 0..2 {
                    for kw in 0..3 {
                        for channel in 0..16 {
                            let ih = oh - 1 + kh * 2;
                            let iw = ow * 2 - 2 + kw;
                            let value = if (0..3).contains(&ih) && (0..5).contains(&iw) {
                                tag((ih * 5 + iw) * 32 + group * 16 + channel)
                            } else {
                                0
                            };
                            want.extend_from_slice(&value.to_le_bytes());
                        }
                    }
                }
            }
        }
    }
    for grant in [vec![0], vec![3, 1, 0, 2]] {
        let mut req = request(&m);
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        req.force = Some("rows".into());
        let plan = call_split("conv.patch_kernel", &args, &req, &m).expect("patch producer");
        validate_ownership(&plan.split.output).expect("patch ownership");
        let (actual, writes) =
            simulate_named_inputs(&plan.kernel, "patches", &|_, _, _, _, _| 0, &tag);
        assert_eq!(actual, want);
        assert!(
            writes.iter().all(|&n| n == 1),
            "one patch producer per destination word"
        );
    }
    // Replace the intermediate grant with the bulk window emitter. The global
    // input batch origin, spatial halo, K padding and row capacity all differ.
    let bulk = json!({"spec": {
        "batch": 3, "height": 3, "width": 5, "cin": 16, "cout": 32,
        "kh": 3, "kw": 3, "stride": [1, 2], "padding": [1, 1],
        "dilation": [1, 1], "groups": 1
    }, "first_batch": 1, "batches": 1, "capacity": 12, "scratch_space": "stage"});
    let kernel = ktpu_kernels::call("conv.unfold_kernel", &bulk, &m).expect("bulk batch view");
    let (actual, _) = simulate_named_inputs(&kernel, "out", &|_, _, _, _, _| 0, &tag);
    let mut expected = Vec::new();
    for row in 0..12 {
        for col in 0..160 {
            let kh = col / 48;
            let kw = col / 16 % 3;
            let ih = row / 3 - 1 + kh;
            let iw = row % 3 * 2 - 1 + kw;
            let bits = if row < 9 && col < 144 && (0..3).contains(&ih) && (0..5).contains(&iw) {
                tag((15 + ih * 5 + iw) * 16 + col % 16)
            } else {
                0
            };
            expected.extend_from_slice(&bits.to_le_bytes());
        }
    }
    assert_eq!(actual, expected);
    assert!(kernel
        .graph
        .scratch
        .iter()
        .all(|buffer| buffer.spec.space == ktpu_graph::BufSpace::Stage));
    for (key, value) in [("first_batch", 3), ("capacity", 8), ("batches", 0)] {
        let mut invalid = bulk.clone();
        invalid[key] = json!(value);
        assert_eq!(
            ktpu_kernels::call("conv.unfold_kernel", &invalid, &m)
                .expect_err("invalid bulk extent")
                .clause,
            "API.OP.001"
        );
    }
}

#[test]
fn conv_product_splits_preserve_output_and_write_each_word_once() {
    let m = machine();
    for groups in [1, 4] {
        let (height, width, cin, cout) = if groups == 1 {
            (8, 8, 32, 128)
        } else {
            (4, 4, 64, 128)
        };
        let rows = height * width;
        let columns = cout / groups;
        let args = json!({
            "spec": {
                "batch": 1, "height": height, "width": width, "cin": cin, "cout": cout,
                "kh": 3, "kw": 3, "stride": [1, 1], "padding": [1, 1],
                "dilation": [1, 1], "groups": groups
            },
            "product": {
                "e": {"nodes": [{"load": 0}], "root": 0}, "extras": [],
                "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": columns, "rows": groups * rows},
                "ring_depth": 2, "tail_bands": true
            }
        });
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![0], &m).expect("reference grant");
        req.force = Some("spatial".into());
        let baseline = call_split("conv.kernel", &args, &req, &m).expect("P=1 convolution");
        assert!(baseline
            .split
            .cost
            .local_cycles
            .iter()
            .all(|&cycles| cycles > 0));
        assert_eq!(
            baseline.split.cost.local_cycles.len(),
            baseline.split.active_nodes,
            "every active convolution owner has local timing"
        );
        let (want, baseline_writes) = simulate_named_inputs(
            &baseline.kernel,
            "c2_out",
            &|_, g, b, l, k| 0x2800 | ((g + b + l + k) as u16 & 0x3ff),
            &|i| 0x3000 | (i as u16 & 0x3ff),
        );
        assert!(baseline_writes.iter().all(|&n| n == 1));
        for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
            req.nodes = NodeSet::new(grant, &m).expect("grant");
            let axes = if groups == 1 {
                vec!["channels", "spatial"]
            } else {
                vec!["groups"]
            };
            for axis in axes {
                req.force = Some(axis.into());
                let plan = call_split("conv.kernel", &args, &req, &m).expect("forced convolution");
                validate_ownership(&plan.split.output).expect("output owners");
                let (actual, writes) = simulate_named_inputs(
                    &plan.kernel,
                    "c2_out",
                    &|_, g, b, l, k| 0x2800 | ((g + b + l + k) as u16 & 0x3ff),
                    &|i| 0x3000 | (i as u16 & 0x3ff),
                );
                assert_eq!(actual, want, "{groups} groups, {axis}");
                assert!(writes.iter().all(|&n| n == 1), "{groups} groups, {axis}");
            }
        }
    }
}

#[test]
fn groupnorm_domains_and_population_trees_own_each_output_word() {
    let m = machine();
    let tag = |i: i64| 0x3c00u16 | ((i * 37 + i / 11) as u16 & 0x3ff);
    let value = |i: i64| ktpu_sim::num::value(ktpu_sim::num::from_f16(tag(i))) as f32;
    for (batch, channels, groups, spatial, option) in
        [(2, 32, 8, 8, "domains"), (1, 2, 1, 85, "spatial")]
    {
        let domains = batch * groups;
        let width = channels / groups * spatial;
        let padded_width = (width + 31) / 32 * 32;
        let eps = 0.00001;
        let args = json!({"spec": {
            "batch": batch, "channels": channels, "height": 1, "width": spatial,
            "groups": groups, "eps": eps
        }});
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![0], &m).expect("reference grant");
        req.force = Some("domains".into());
        let baseline = call_split("groupnorm.kernel", &args, &req, &m).expect("P=1 GroupNorm");
        let (reference_bytes, writes) =
            simulate_named_inputs(&baseline.kernel, "out", &|_, _, _, _, _| 0, &tag);
        assert!(writes.iter().all(|&n| n == 1));
        let logical = |bytes: &[u8]| {
            let all = decoded(bytes);
            (0..domains)
                .flat_map(|d| {
                    let start = (d * padded_width) as usize;
                    all[start..start + width as usize].to_vec()
                })
                .collect::<Vec<_>>()
        };
        let reference = logical(&reference_bytes);
        let mut f32_reference = Vec::new();
        for d in 0..domains {
            let xs: Vec<_> = (0..width).map(|j| value(d * width + j)).collect();
            let mean = xs.iter().sum::<f32>() / width as f32;
            let variance = xs.iter().map(|x| (x - mean) * (x - mean)).sum::<f32>() / width as f32;
            let inverse = (variance + eps as f32).sqrt().recip();
            for (j, x) in xs.into_iter().enumerate() {
                let channel = (d % groups) * (channels / groups) + j as i64 / spatial;
                f32_reference.push((x - mean) * inverse * value(channel) + value(channel));
            }
        }
        eprintln!(
            "GroupNorm {option} P=1 vs f32: {}",
            error_statistics(&reference, &f32_reference)
        );
        for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
            let p = grant.len();
            req.nodes = NodeSet::new(grant, &m).expect("grant");
            req.force = Some(option.into());
            let plan = call_split("groupnorm.kernel", &args, &req, &m).expect("forced GroupNorm");
            validate_ownership(&plan.split.output).expect("output lines");
            assert_eq!(plan.split.active_nodes, p);
            let (bytes, writes) =
                simulate_named_inputs(&plan.kernel, "out", &|_, _, _, _, _| 0, &tag);
            assert!(writes.iter().all(|&n| n == 1), "{option}, P={p}");
            let actual = logical(&bytes);
            if option == "domains" {
                assert_eq!(bytes, reference_bytes, "complete domains, P={p}");
            }
            eprintln!(
                "GroupNorm {option} P={p} vs P=1: {}; vs f32: {}",
                error_statistics(&actual, &reference),
                error_statistics(&actual, &f32_reference)
            );
        }
    }
}

#[test]
fn destination_cast_preserves_global_element_origins_and_exact_tail() {
    let m = machine();
    let hw = ktpu_kernels::gen::Hw::of(&m);
    let args = json!({"n": 4 * hw.vlmax + hw.lanes, "src": "f16", "dst": "f32"});
    let baseline = ktpu_kernels::call("convert.cast_kernel", &args, &m).expect("P=1 cast");
    let (want, _) = simulate_matmul(&baseline);
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let mut req = request(&m);
        let p = grant.len();
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        req.force = Some("destination-tiles".into());
        let got = call_split("convert.cast_kernel", &args, &req, &m).expect("cast owners");
        assert_eq!(got.split.active_nodes, p);
        validate_ownership(&got.split.output).expect("physical owners");
        let (actual, writes) = simulate_matmul(&got.kernel);
        assert_eq!(actual, want);
        assert!(writes.iter().all(|&count| count == 1));
    }
}

#[test]
fn destination_pack_tail_matches_single_node_and_writes_each_word_once() {
    let m = machine();
    for (side, strategy, rows, cols, rp, cp, source) in [
        ("a", "native", 15, 48, 16, 128, 0),
        ("b", "native", 3, 48, 4, 256, 0),
        ("a", "staged", 3, 48, 4, 256, 0),
        ("b", "native", 7, 112, 8, 256, 1),
        ("a", "native", 7, 112, 8, 256, 2),
    ] {
        let mut args = json!({
            "src": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": cols, "rows": rows},
            "rows": rows, "cols": cols, "rp": rp, "cp": cp,
            "side": side, "g": 1, "nk": 2, "batches": 1, "finite": source == 0,
            "pad_strategy": strategy
        });
        let generator = if source == 1 {
            args["src_bytes"] = json!(rows * 256);
            args["levels"] = json!([[rows, 256], [cols / 16, -32]]);
            args["at"] = json!(224);
            "matmul.quant_walk_kernel"
        } else {
            if source == 2 {
                args["prepared"] = json!([{"made_by": {
                    "gen": "relayout.plan",
                    "args": {
                        "src_n": rows * cols, "m": rows * cols,
                        "walk": [[rows, cols], [cols, -1]], "at": cols - 1,
                        "dwalk": [[rows * cols, 1]], "dat": 0
                    }
                }}]);
                args["composed"] = json!([0]);
            }
            "matmul.quant_kernel"
        };
        let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
        let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
            simulate_named_inputs(kernel, "q", &|_, _, _, _, _| 0, &input)
        };
        let baseline = ktpu_kernels::call(generator, &args, &m).expect("P=1 pack");
        let (want, baseline_writes) = simulate(&baseline);
        assert!(baseline_writes.iter().all(|&count| count == 1));
        for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
            let mut req = request(&m);
            let p = grant.len();
            req.nodes = NodeSet::new(grant, &m).expect("grant");
            req.force = Some("destination-tiles".into());
            let got = call_split(generator, &args, &req, &m).expect("pack owners");
            assert_eq!(got.split.active_nodes, p);
            validate_ownership(&got.split.output).expect("complete scale and entry owners");
            let (actual, writes) = simulate(&got.kernel);
            assert_eq!(actual, want);
            assert!(writes.iter().all(|&count| count == 1));
        }
    }
}

#[test]
fn destination_transpose_matches_single_node_and_writes_each_word_once() {
    let m = machine();
    let planes = json!({
        "ports": [
            {"name": "x", "shape": [34, 32], "dtype": "f16", "written": false},
            {"name": "mask", "shape": [2, 128], "dtype": "f16", "written": false},
            {"name": "y", "shape": [64, 32], "dtype": "f16", "written": true}
        ], "scratch": [], "staged": [], "scratch_space": "stage",
        "steps": [{"kind": "planes", "planes": {
            "src": "x", "dst": "y", "mask": "mask", "batches": 2, "groups": 2,
            "rows": 17, "columns": 16, "padded_rows": 32,
            "source_offset": 0, "batch_stride": 17 * 32,
            "group_stride": 16, "row_stride": 32
        }}]
    });
    let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
    let expected: Vec<_> = (0..4 * 16 * 32)
        .flat_map(|i| {
            let plane = i / (16 * 32);
            let col = i / 32 % 16;
            let row = i % 32;
            let value = if row < 17 {
                input((plane / 2 * 17 + row) * 32 + plane % 2 * 16 + col)
            } else {
                0
            };
            value.to_le_bytes()
        })
        .collect();
    let mut singleton = planes.clone();
    singleton["steps"][0]["planes"]["rows"] = json!(1);
    singleton["steps"][0]["planes"]["row_stride"] = json!(1i64 << 62);
    let bounded = ktpu_kernels::call("fused.kernel", &singleton, &m)
        .expect("singleton strides never advance");
    ktpu_graph::program(&bounded.graph, &m).expect("no overflowing byte stride");
    let local = ktpu_kernels::call("fused.kernel", &planes, &m).expect("strided plane transpose");
    let mut req = request(&m);
    req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("plane owners");
    req.force = Some("planes".into());
    let distributed = call_split("fused.kernel", &planes, &req, &m).expect("plane partition");
    assert_eq!(distributed.split.active_nodes, 4);
    validate_ownership(&distributed.split.output).expect("one plane writer");
    for (index, shard) in distributed.split.shards.iter().enumerate() {
        for row in 0..17 {
            let start = ((index / 2 * 17 + row) * 32 + index % 2 * 16) as u64 * 2;
            assert!(shard.read_regions.iter().any(|r| {
                r.allocation == "x" && r.bytes.start == start && r.bytes.end == start + 32
            }));
        }
    }
    for kernel in [&local, &distributed.kernel] {
        let (actual, writes) = simulate_named_inputs(kernel, "y", &|_, _, _, _, _| 0, &input);
        assert_eq!(actual, expected);
        assert!(writes.iter().all(|&count| count == 1));
    }
    // Extra key tiles are masked. The inverse layout preserves other public
    // batches/heads, including when independent stages have separate owners.
    let mut attention_outputs = Vec::new();
    for (keys, blocked) in [(64, false), (192, true)] {
        let args = json!({
            "ports": [
                {"name": "q", "shape": [128, 64], "dtype": "f16", "written": false},
                {"name": "k", "shape": [2 * keys, 64], "dtype": "f16", "written": false},
                {"name": "vt", "shape": [128, keys], "dtype": "f16", "written": false},
                {"name": "tail", "shape": [16], "dtype": "f16", "written": false},
                {"name": "y", "shape": [51, 192], "dtype": "f16", "written": true}
            ], "scratch": [{"name": "result", "shape": [128, 64], "dtype": "f16"}],
            "staged": [], "scratch_space": "stage",
            "steps": [
                {"kind": "attention", "attention": {
                    "q": "q", "k": "k", "vt": "vt", "out": "result", "tail": "tail",
                    "heads": 2, "queries": 64, "keys": keys, "dim": 64,
                    "query_tile": 16, "query_cores": 2, "key_tile": 64, "valid_keys": 17,
                    "block_rows": 32, "blocked": blocked, "compact_mask": true,
                    "head_group": if blocked { 0 } else { 1 }
                }},
                {"kind": "planes", "planes": {
                    "scatter": true, "src": "result", "dst": "y", "mask": null,
                    "batches": 2, "groups": 1, "rows": 17, "columns": 64,
                    "padded_rows": 64, "source_offset": 17 * 192 + 64,
                    "batch_stride": 17 * 192, "group_stride": 64, "row_stride": 192
                }}
            ]
        });
        if !blocked {
            let mut masks = args.clone();
            masks["ports"].as_array_mut().expect("ports").extend([
                json!({"name": "kw", "shape": [384, 64], "dtype": "f16", "written": false}),
                json!({"name": "vw", "shape": [128, 192], "dtype": "f16", "written": false}),
            ]);
            masks["scratch"]
                .as_array_mut()
                .expect("scratch")
                .push(json!({"name": "discard", "shape": [128, 64], "dtype": "f16"}));
            masks["steps"][0]["attention"]["valid_keys"] = json!(64);
            let mut first = masks["steps"][0].clone();
            first["attention"]["k"] = json!("kw");
            first["attention"]["vt"] = json!("vw");
            first["attention"]["keys"] = json!(192);
            first["attention"]["out"] = json!("discard");
            masks["steps"]
                .as_array_mut()
                .expect("steps")
                .insert(0, first);
            let masks = ktpu_kernels::call("fused.kernel", &masks, &m)
                .expect("distinct compact mask domains");
            let body = serde_json::to_value(&masks.graph.body).expect("mask schedule");
            let initialized: Vec<_> = body
                .as_array()
                .expect("body")
                .iter()
                .filter_map(|stmt| {
                    let attrs = stmt.get("kw")?.as_array()?;
                    (stmt["op"] == "gt4"
                        && attrs.iter().any(|attr| {
                            attr[0] == "walk"
                                && attr[1] == json!({"walk": [[1, 0], [1, 0], [4, 32], [4, 0]]})
                        }))
                    .then(|| stmt["dst"]["buffer"].clone())
                })
                .collect();
            assert_eq!(
                initialized.len(),
                2,
                "different score domains initialize different masks"
            );
        }
        let mut mutated_tail = args.clone();
        mutated_tail["steps"]
            .as_array_mut()
            .unwrap()
            .insert(1, json!({"kind": "copy", "src": "tail", "dst": "tail"}));
        let error = ktpu_kernels::call("fused.kernel", &mutated_tail, &m)
            .expect_err("a cached tail cannot be a package write destination");
        assert_eq!(error.clause, "API.OP.001");
        let mut aliased_merge = args.clone();
        aliased_merge["steps"][0]["attention"]["merge"] = args["steps"][1]["planes"].clone();
        aliased_merge["steps"][0]["attention"]["merge"]["dst"] = json!("k");
        let error = ktpu_kernels::call("fused.kernel", &aliased_merge, &m)
            .expect_err("streaming output cannot overwrite keys");
        assert_eq!(error.clause, "API.OP.001");
        let kernel = if blocked {
            ktpu_kernels::call("fused.kernel", &args, &m)
                .expect("materialized blocked attention and partial inverse planes")
        } else {
            let mut grouped = args.clone();
            grouped["ports"][0]["name"] = json!("group0_pq");
            let mut steps = Vec::new();
            for (source, private, rows, cols) in [
                ("group0_pq", "pq", 64, 64),
                ("k", "pk", keys, 64),
                ("vt", "pv", 64, keys),
            ] {
                grouped["scratch"]
                    .as_array_mut()
                    .expect("scratch")
                    .push(json!({
                        "name": private, "shape": [2 * rows, cols], "dtype": "f16"
                    }));
                steps.push(json!({"kind": "planes", "planes": {
                    "src": source, "dst": private, "mask": null,
                    "batches": 2, "groups": 1, "rows": rows, "columns": cols,
                    "padded_rows": rows, "source_offset": 0,
                    "batch_stride": rows * cols, "group_stride": rows * cols,
                    "row_stride": cols
                }}));
            }
            let mut attention = args["steps"][0].clone();
            attention["attention"]["q"] = json!("pq");
            attention["attention"]["k"] = json!("pk");
            attention["attention"]["vt"] = json!("pv");
            steps.push(attention);
            steps.push(args["steps"][1].clone());
            grouped["steps"] = json!(steps);
            grouped["group_ends"] = json!([5]);
            let mut group_request = request(&m);
            group_request.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("permuted grant");
            group_request.force = Some("groups".into());
            let selected = call_split("fused.kernel", &grouped, &group_request, &m)
                .expect("independent planar attention groups");
            assert_eq!(selected.split.active_nodes, 2);
            validate_ownership(&selected.split.output).expect("partial scatter owners");
            for shard in selected
                .split
                .shards
                .iter()
                .filter(|s| s.role == ktpu_kernels::split::ShardRole::Compute)
            {
                assert!(!shard.read_regions.is_empty());
                assert!(!shard.scratch_regions.is_empty());
            }
            selected.kernel
        };
        let seed = vec![0x55u8; 51 * 192 * 2];
        let values = |rows: i64, cols: i64, f: &dyn Fn(i64, i64) -> u16| -> Vec<u8> {
            (0..rows)
                .flat_map(|row| (0..cols).flat_map(move |col| f(row, col).to_le_bytes()))
                .collect()
        };
        let q = values(128, 64, &|r, c| 0x3000 | (((r + c) % 5) as u16 * 32));
        let k = values(2 * keys, 64, &|r, c| {
            0x3000 | (((r % keys + c + r / keys) % 7) as u16 * 32)
        });
        let vt = values(128, keys, &|r, c| {
            0x3000 | (((r % 64 + c + r / 64) % 11) as u16 * 32)
        });
        let tail = values(1, 16, &|_, c| if c == 0 { 0x3c00 } else { 0 });
        let (bytes, writes) = simulate_named_tables(
            &kernel,
            "y",
            &|_, _, _, _, _| 0,
            &input,
            &[
                ("y", seed.clone()),
                (if blocked { "q" } else { "group0_pq" }, q),
                ("k", k),
                ("vt", vt),
                ("tail", tail),
            ],
        );
        for row in 0..51 {
            for word in 0..12 {
                let written = row >= 17 && (4..8).contains(&word);
                let start = (row * 192 + word * 16) * 2;
                assert!(writes[start..start + 32]
                    .iter()
                    .all(|&count| count == u32::from(written)));
                if !written {
                    assert_eq!(&bytes[start..start + 32], &seed[start..start + 32]);
                }
            }
        }
        let value = |bits| ktpu_sim::num::value(ktpu_sim::num::from_f16(bits)) as f32;
        for plane in 0..2 {
            for query in 0..17 {
                let scores: Vec<f32> = (0..17)
                    .map(|key| {
                        (0..64)
                            .map(|col| {
                                let q =
                                    value(0x3000 | (((plane * 64 + query + col) % 5) as u16 * 32));
                                let k = value(0x3000 | (((key + col + plane) % 7) as u16 * 32));
                                q * k
                            })
                            .sum::<f32>()
                            / 8.0
                    })
                    .collect();
                let maximum = scores.iter().copied().fold(f32::NEG_INFINITY, f32::max);
                let weights: Vec<f32> = scores.iter().map(|x| (x - maximum).exp()).collect();
                let sum: f32 = weights.iter().sum();
                for col in 0..64 {
                    let expected: f32 = weights
                        .iter()
                        .enumerate()
                        .map(|(key, weight)| {
                            weight * value(0x3000 | (((col + key + plane) % 11) as u16 * 32))
                        })
                        .sum::<f32>()
                        / sum;
                    let offset = (((plane + 1) * 17 + query) * 192 + 64 + col) * 2;
                    let actual = value(u16::from_le_bytes([bytes[offset], bytes[offset + 1]]));
                    assert!(
                        actual.is_finite() && (actual - expected).abs() < 0.01,
                        "materialized attention {actual} vs independent f32 {expected}"
                    );
                }
            }
        }
        attention_outputs.push(bytes);
    }
    assert_eq!(attention_outputs[0], attention_outputs[1]);
    for (a, b, classed) in [(64, 96, true)] {
        let mut args = json!({"nb": 1, "A": a, "B": b});
        if classed {
            args["shape_class"] = json!({
                "kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"
            });
        }
        let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
        let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
            simulate_named_inputs(kernel, "y", &|_, _, _, _, _| 0, &input)
        };
        let baseline =
            ktpu_kernels::call("transpose.transpose_kernel", &args, &m).expect("P=1 transpose");
        let (want, baseline_writes) = simulate(&baseline);
        assert!(baseline_writes.iter().all(|&count| count == 1));
        for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
            let mut req = request(&m);
            let p = grant.len();
            req.nodes = NodeSet::new(grant, &m).expect("grant");
            req.force = Some("destination-tiles".into());
            let got = call_split("transpose.transpose_kernel", &args, &req, &m)
                .expect("transpose owners");
            assert_eq!(got.split.active_nodes, p);
            validate_ownership(&got.split.output).expect("destination rectangles");
            let (actual, writes) = simulate(&got.kernel);
            assert_eq!(actual, want);
            assert!(writes.iter().all(|&count| count == 1));
        }
    }
}

#[test]
fn destination_reverse_keeps_walk_origins_and_exact_tail() {
    let m = machine();
    let hw = ktpu_kernels::gen::Hw::of(&m);
    let words = 4 * 8 * hw.vlmax / hw.lanes + 3;
    let args = json!({
        "src_bytes": (words + 1) * hw.word_bytes,
        "levels": [[words, -hw.word_bytes]],
        "at": words * hw.word_bytes,
        "n": words * hw.lanes
    });
    let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(kernel, "y", &|_, _, _, _, _| 0, &|i| {
            0x2800u16 | ((i * 37) as u16 & 0x3ff)
        })
    };
    let baseline = ktpu_kernels::call("transpose.reverse_kernel", &args, &m).expect("P=1 reverse");
    let (want, _) = simulate(&baseline);
    for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
        let mut req = request(&m);
        let p = grant.len();
        req.nodes = NodeSet::new(grant, &m).expect("grant");
        req.force = Some("destination-tiles".into());
        let got = call_split("transpose.reverse_kernel", &args, &req, &m).expect("reverse owners");
        assert_eq!(got.split.active_nodes, p);
        validate_ownership(&got.split.output).expect("reverse destination owners");
        let (actual, writes) = simulate(&got.kernel);
        assert_eq!(actual, want);
        assert!(writes.iter().all(|&count| count == 1));
    }
}

#[test]
fn destination_mover_walk_and_fill_preserve_written_windows() {
    let m = machine();
    let hw = ktpu_kernels::gen::Hw::of(&m);
    let words = 131;
    let base = hw.word_bytes;
    let args = json!({
        "src_bytes": (words + 1) * hw.word_bytes,
        "levels": [[words, -hw.word_bytes]], "at": words * hw.word_bytes,
        "dst_bytes": (words + 2) * hw.word_bytes,
        "dlevels": [[words, hw.word_bytes]], "dat": base,
        "value": 0x1234, "ew": 16
    });
    let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
        simulate_named_inputs(kernel, "dst", &|_, _, _, _, _| 0, &|i| {
            0x2800u16 | ((i * 37) as u16 & 0x3ff)
        })
    };
    for (stride, classed, permuted) in [
        (hw.word_bytes, false, false),
        (-hw.word_bytes, false, false),
        (2 * hw.word_bytes, false, false),
        (hw.word_bytes, true, false),
        (hw.word_bytes, false, true),
    ] {
        let mut args = args.clone();
        args["dlevels"] = json!([[words, stride]]);
        args["dst_bytes"] = json!((words + 2) * stride.abs());
        args["dat"] = json!(if stride < 0 { words * -stride } else { base });
        if permuted {
            args["levels"] = json!([[4, 32 * hw.word_bytes], [32, -hw.word_bytes]]);
            args["at"] = json!(31 * hw.word_bytes);
            args["dlevels"] = json!([[4, -hw.word_bytes], [32, 4 * hw.word_bytes]]);
            args["dat"] = json!(4 * hw.word_bytes);
        }
        if classed {
            args["shape_class"] = json!({
                "kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"
            });
        }
        for generator in ["convert.walk_kernel", "convert.fill_kernel"] {
            if classed && generator == "convert.fill_kernel" {
                continue;
            }
            let baseline = ktpu_kernels::call(generator, &args, &m).expect("P=1 mover");
            let (want, want_writes) = simulate(&baseline);
            for grant in [vec![3, 1], vec![3, 1, 0, 2]] {
                let mut req = request(&m);
                let p = grant.len();
                req.nodes = NodeSet::new(grant, &m).expect("grant");
                req.force = Some("destination-tiles".into());
                let got = call_split(generator, &args, &req, &m).expect("mover owners");
                assert_eq!(got.split.active_nodes, p);
                validate_ownership(&got.split.output).expect("mover destination owners");
                let (actual, writes) = simulate(&got.kernel);
                assert_eq!(actual, want);
                assert_eq!(writes, want_writes);
                assert!(writes.iter().all(|&count| count <= 1));
                assert!(writes.iter().any(|&count| count == 1));
            }
        }
    }
}

#[test]
fn destination_indexed_schedules_keep_one_owner_for_layered_writes() {
    let m = machine();
    let plan = json!({
        "src_n": 256, "walk": [[241, 1]], "at": 0,
        "dwalk": [[241, 1]], "dat": 3, "m": 256, "keep": true
    });
    for (generator, planner) in [
        ("lanes.relayout_kernel", "lanes.plan"),
        ("relayout.relayout_kernel", "relayout.plan"),
    ] {
        let (index, masks): (Vec<f32>, Vec<u16>) = if planner == "lanes.plan" {
            let pl = ktpu_kernels::call::lanes_plan(&plan, &m).expect("lane plan");
            (pl.index, pl.masks)
        } else {
            let pl = ktpu_kernels::call::relayout_plan(&plan, &m)
                .expect("relayout plan")
                .expect("planned runs");
            (
                pl.index,
                pl.masks
                    .into_iter()
                    .flatten()
                    .map(|x| ktpu_sim::num::to_f16(ktpu_sim::num::from_f32(x.to_bits())))
                    .collect(),
            )
        };
        let tables = [
            (
                "index",
                index.iter().flat_map(|x| x.to_le_bytes()).collect(),
            ),
            ("mask", masks.iter().flat_map(|x| x.to_le_bytes()).collect()),
        ];
        let args = json!({"pl": {"made_by": {"gen": planner, "args": plan}}});
        let simulate = |kernel: &ktpu_kernels::kernel::Kernel| {
            simulate_named_tables(
                kernel,
                "dst",
                &|_, _, _, _, _| 0,
                &|i| 0x2800u16 | ((i * 37) as u16 & 0x3ff),
                &tables,
            )
        };
        let baseline = ktpu_kernels::call(generator, &args, &m).expect("local indexed schedule");
        let (want, want_writes) = simulate(&baseline);
        let mut req = request(&m);
        req.nodes = NodeSet::new(vec![3, 1, 0, 2], &m).expect("grant");
        req.force = Some("p1".into());
        let got = call_split(generator, &args, &req, &m).expect("explicit one-owner option");
        validate_ownership(&got.split.output).expect("indexed allocation owner");
        let (actual, writes) = simulate(&got.kernel);
        assert_eq!(actual, want);
        assert_eq!(writes, want_writes);
        for (byte, &count) in writes.iter().enumerate() {
            if count != 0 {
                let owners: Vec<_> = got
                    .split
                    .output
                    .ownership
                    .iter()
                    .filter(|(_, region)| {
                        region.allocation == "dst" && region.bytes.contains(&(byte as u64))
                    })
                    .map(|(owner, _)| *owner)
                    .collect();
                assert_eq!(
                    owners,
                    vec![0],
                    "each executed write has one certified owner"
                );
            }
        }
        req.force = Some("destination-tiles".into());
        let error = call_split(generator, &args, &req, &m).expect_err("no table repartition");
        assert!(error.to_string().contains("p1"));
    }
}

#[test]
fn classed_lanes_publish_selected_lanes_and_preserve_the_rest() {
    let m = machine();
    let plan = ktpu_kernels::gen::lanes::plan(
        256,
        &[(16, 16), (8, 1)],
        0,
        &[(16, 16), (8, 1)],
        8,
        256,
        true,
        &m,
    )
    .expect("kept lane plan");
    let tables = ktpu_kernels::gen::classes::lanes::tables(&plan, &m);
    let table = tables.first().expect("lane class");
    let classed = ktpu_kernels::gen::classes::lanes::kernel(
        &table.class,
        plan.src_words,
        plan.dst_words,
        ktpu_kernels::gen::classes::Cores::All,
        &m,
    )
    .expect("classed lane kernel");
    let kept = |i: i64| 0x3000u16 | ((i * 13) as u16 & 0x3ff);
    let inputs = [
        (
            "index",
            table.index.iter().flat_map(|x| x.to_le_bytes()).collect(),
        ),
        (
            "mask",
            table.masks.iter().flat_map(|x| x.to_le_bytes()).collect(),
        ),
        (
            "dst",
            (0..256).flat_map(|i| kept(i).to_le_bytes()).collect(),
        ),
    ];
    let seed = |i: i64| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
    let (actual, _) =
        simulate_named_tables(&classed.kernel, "dst", &|_, _, _, _, _| 0, &seed, &inputs);
    for (i, value) in actual.chunks_exact(2).enumerate() {
        let expected = if i % 16 < 8 {
            kept(i as i64)
        } else {
            seed(i as i64 - 8)
        };
        assert_eq!(u16::from_le_bytes([value[0], value[1]]), expected);
    }
}
