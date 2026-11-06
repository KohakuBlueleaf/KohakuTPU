//! Kernel split contracts exercised by the DSL stage.

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
    for (nbytes, classed) in [(32, false), (289, false), (289, true)] {
        let mut args = json!({"nbytes": nbytes});
        if classed {
            args["shape_class"] = json!({
                "kblock": 32, "max_block": 256, "policy": "exact", "cores": "min"
            });
        }
        let baseline = ktpu_kernels::call("convert.copy_kernel", &args, &m).expect("P=1");
        let input = |i| 0x2800u16 | ((i * 37) as u16 & 0x3ff);
        let (want, _) = simulate_named_inputs(&baseline, "dst", &|_, _, _, _, _| 0, &input);
        for grant in [vec![3], vec![3, 1], vec![3, 1, 0, 2]] {
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
    simulate_named_executable(kernel, None, output, input, extra, tables)
}

fn simulate_matmul_executable(
    kernel: &ktpu_kernels::kernel::Kernel,
    l1: Option<&str>,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
) -> (Vec<u8>, Vec<u32>) {
    simulate_named_executable(kernel, l1, "out", input, extra, &[])
}

fn simulate_named_executable(
    kernel: &ktpu_kernels::kernel::Kernel,
    l1: Option<&str>,
    output: &str,
    input: &dyn Fn(bool, i64, i64, i64, i64) -> u16,
    extra: &dyn Fn(i64) -> u16,
    tables: &[(&str, Vec<u8>)],
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
        let card = machine();
        let facts = Facts::of(&card);
        let bound: BTreeMap<_, _> = binds
            .iter()
            .map(|(k, v)| (k.clone(), i128::from(*v)))
            .collect();
        let programs = ktpu_l1::build::programs(
            executable,
            &text.name,
            &bound,
            &card,
            Some(card.nodes()),
            None,
        )
        .expect("bound delivery programs");
        let builder = ktpu_l1::build::Builder::new(&card).expect("package builder");
        let packages: BTreeMap<_, _> = programs
            .iter()
            .map(|p| {
                (
                    p.mesh,
                    builder
                        .build(p, ktpu_l1::build::Opts::default())
                        .expect("delivery package")
                        .build(false, false),
                )
            })
            .collect();
        let target = ktpu_verify::Target::from_machine(&card, Some(card.nodes())).expect("target");
        let findings = ktpu_verify::check(&[packages], &target);
        assert!(!findings.iter().any(|f| f.kind.refuses()), "{findings:?}");
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
                simulate_matmul_executable(&plan.kernel, Some(executable), &input, &extra);
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
    for grant in [vec![0], vec![3, 1], vec![3, 1, 0, 2]] {
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
    for (a, b, classed) in [(64, 96, false), (48, 128, false), (64, 96, true)] {
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
