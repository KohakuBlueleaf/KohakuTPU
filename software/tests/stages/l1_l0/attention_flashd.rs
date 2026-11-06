//! Direct-ring shares execute twice, including a four-PV-tile tail. Numerical
//! differences are statistics; the structural verdict is completion/ownership.

use std::collections::BTreeMap;
use std::path::Path;
use std::rc::Rc;

use ktpu_isa::{Isa, UnitKind};
use ktpu_kernels::call_split;
use ktpu_kernels::split::{
    validate_ownership, DispatchProfile, NodeSet, NumericContract, OutputRequirement, SplitRequest,
};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::read;
use ktpu_sim::l0;
use ktpu_sim::machine::Machine;
use ktpu_sim::{num, Dram};
use serde_json::json;

use super::attention_flash::{coords, execute_seeded, seed_values};

fn half(bits: u16) -> f32 {
    num::value(num::from_f16(bits)) as f32
}

fn operand(
    heads: usize,
    salt: u32,
    rows: usize,
    cols: usize,
    g: usize,
    nk: usize,
    magnitude: u16,
    positive: bool,
) -> Vec<f32> {
    let mut out = vec![0.0; heads * rows * cols];
    for h in 0..heads {
        for i in 0..512 {
            let values = seed_values(salt, h * 512 + i, magnitude, positive);
            let block = i % nk;
            let group = (i / nk) % g;
            let chunk = (i / (nk * g)) % (cols / (32 * nk));
            let tile = i / (g * (cols / 32));
            for (lane, values) in values.iter().enumerate() {
                for (k, &bits) in values.iter().enumerate() {
                    let row = (tile * g + group) * 4 + lane;
                    let col = (chunk * nk + block) * 32 + k;
                    out[(h * rows + row) * cols + col] = half(bits);
                }
            }
        }
    }
    out
}

fn reference(heads: usize, queries: usize) -> Vec<f32> {
    let q = operand(heads, 0x12345678, 1024, 64, 32, 2, 0x2800, false);
    let k = operand(heads, 0x87654321, 1024, 64, 128, 2, 0x2800, false);
    let v = operand(heads, 0x31415926, 64, 1024, 16, 16, 0x3800, true);
    let mut out = vec![0.0; heads * queries * 64];
    for h in 0..heads {
        for r in 0..queries {
            let mut scores = vec![0.0f32; 1024];
            for (t, score) in scores.iter_mut().enumerate() {
                for d in 0..64 {
                    *score += q[(h * 1024 + r) * 64 + d] * k[(h * 1024 + t) * 64 + d];
                }
            }
            let maximum = scores.iter().copied().fold(f32::NEG_INFINITY, f32::max);
            for score in &mut scores {
                *score = (*score - maximum).exp2();
            }
            let sum: f32 = scores.iter().sum();
            for d in 0..64 {
                out[(h * queries + r) * 64 + d] = scores
                    .iter()
                    .enumerate()
                    .map(|(t, &p)| p * v[(h * 64 + d) * 1024 + t])
                    .sum::<f32>()
                    / sum;
            }
        }
    }
    out
}

fn output(bytes: &[u8], heads: usize, queries: usize) -> Vec<f32> {
    let mut result = Vec::new();
    for h in 0..heads {
        for r in 0..queries {
            for d in 0..64 {
                let sub = (r / 32) * 128 + (r % 32 / 4) * 16 + d / 4;
                let at = h * 131072 + sub * 32 + (r % 4 * 4 + d % 4) * 2;
                result.push(half(u16::from_le_bytes([bytes[at], bytes[at + 1]])));
            }
        }
    }
    result
}

fn statistics(label: &str, got: &[f32], reference: &[f32]) {
    let mut absolute = Vec::new();
    let mut relative = Vec::new();
    let mut zero_reference = Vec::new();
    let mut nonfinite = 0;
    for (&x, &y) in got.iter().zip(reference) {
        if !x.is_finite() || !y.is_finite() {
            nonfinite += 1;
        } else {
            let error = (x - y).abs();
            absolute.push(error);
            if y == 0.0 {
                zero_reference.push(error);
            } else {
                relative.push(error / y.abs());
            }
        }
    }
    println!("{label}: nonfinite={nonfinite}");
    for (kind, mut values) in [
        ("abs", absolute),
        ("rel", relative),
        ("zero-ref abs", zero_reference),
    ] {
        if !values.is_empty() {
            values.sort_by(f32::total_cmp);
            let n = values.len();
            println!(
                "{label} {kind}: max={} mean={} median={}",
                values[n - 1],
                values.iter().sum::<f32>() / n as f32,
                (values[(n - 1) / 2] + values[n / 2]) / 2.0,
            );
        } else {
            println!("{label} {kind}: no finite comparisons");
        }
    }
}

#[test]
fn direct_ring_splits_close_credits_and_report_p1_and_f32_statistics() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    let machine = ktpu_machine::Machine::load(root.join("machines/multimesh_v9t8.json"))
        .expect("four-node machine");
    let (heads, queries) = (2, 384);
    let f32_reference = reference(heads, queries);
    let mut p1 = Vec::new();
    for (grant, axis) in [
        (vec![0], "heads"),
        (vec![1, 0], "heads"),
        (vec![3, 1, 0, 2], "query-rows"),
        (vec![3, 1, 0, 2], "heads-query-2x2"),
    ] {
        let p = grant.len();
        let request = SplitRequest {
            nodes: NodeSet::new(grant, &machine).expect("grant"),
            consumer: OutputRequirement::default(),
            dispatch: DispatchProfile::Common,
            numerics: NumericContract::default(),
            input_forms: vec![],
            force: Some(axis.into()),
        };
        if p == 1 {
            let error = call_split("attention.flashd", &json!({}), &request, &machine)
                .err()
                .expect("direct-ring admission requires a numerical contract");
            assert_eq!(error.clause, "API.OP.001");
            assert!(error.message.contains("unit-scores"));
        }
        let plan = call_split(
            "attention.flashd",
            &json!({"heads": heads, "queries": queries, "contract": "unit-scores"}),
            &request,
            &machine,
        )
        .expect("direct-ring template");
        validate_ownership(&plan.split.output).expect("disjoint destination lines");
        println!("P={p} {axis}");
        for shard in &plan.split.shards {
            println!(
                "mesh={} head/query tile ranges={:?}",
                request.nodes.ids()[shard.logical_node],
                shard.tile_ranges,
            );
        }
        let bytes = execute_seeded(
            plan.l1.as_deref().expect("L1"),
            &machine,
            p as u32,
            heads,
            queries,
            0x2800,
            Some(0x3800),
            // One replay of the four-sysnode 2x2 plan witnesses that credits close
            // across repetitions; the other plans run once.
            if axis == "heads-query-2x2" { 2 } else { 1 },
        );
        let got = output(&bytes, heads, queries);
        statistics(&format!("P={p} {axis} vs f32"), &got, &f32_reference);
        if p == 1 {
            p1 = got;
        } else {
            statistics(&format!("P={p} {axis} vs P=1"), &got, &p1);
        }
    }
}

#[test]
fn an_undercredited_emit_cannot_complete_its_collecting_mark() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    let card =
        ktpu_machine::Machine::load(root.join("machines/multimesh_v9t8.json")).expect("machine");
    let module = read(
        r#"target ktpu.v9
image credits()
    vpost mg[0] n=7
    halt
fn starved.l1()
    send vc[0]
        run credits()
    send mg[0]
        gemm 1x8x1 a=A0 b=B0 emit to=vc[0] l1=0 count credit
        drain n=8 to=vc[0] l1=0 fused count credit
    e = mark mg[0]
    wait mg[0] upto=e
    barrier
"#,
        "undercredited emission",
    )
    .expect("L1 stream");
    let nodes =
        programs(&module, "starved", &BTreeMap::new(), &card, Some(1), None).expect("node program");
    let builder = Builder::new(&card).expect("builder");
    let mut cores = Cores::new(&card).expect("images");
    let (package, _) = builder
        .package(
            &nodes[0],
            Opts {
                fetch: Some((card.die.fetch_port.x(), card.die.fetch_port.y())),
                cores: Some(&mut cores),
                relocate: false,
            },
        )
        .expect("ENGINE package");
    let facts = ktpu_l0::Facts::of_mesh(&card, nodes[0].mesh).expect("mesh facts");
    let units = l0::Units {
        vc: coords(&card, UnitKind::Vc),
        mg: coords(&card, UnitKind::Mg),
        src_mover: facts.src_mover(),
        counter_bits: facts.counter_bits,
    };
    let mut machine = Machine::new(
        Dram::new(0),
        card.units(UnitKind::Vc).len(),
        card.units(UnitKind::Mg).len(),
        128,
        7,
    );
    machine.isa = Some(Rc::new(Isa::reference(&card).expect("ISA")));
    let error = l0::run(&package, &units, &mut machine)
        .expect_err("seven credits cannot hand off an eight-granule burst");
    assert!(
        error.contains("Mg(0) waits on what no unit gives"),
        "the stream must block on its credit debt, not on decoding: {error}"
    );
}
