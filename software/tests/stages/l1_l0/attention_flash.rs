//! Independent head/query owners preserve the reference L1 instruction stream.

use std::collections::BTreeMap;
use std::path::Path;
use std::rc::Rc;

use ktpu_isa::{Isa, Machine as Card, UnitKind};

use ktpu_kernels::call_split;
use ktpu_kernels::gen::attention::flash::TEMPLATE;
use ktpu_kernels::split::{
    validate_ownership, DispatchProfile, NodeSet, NumericContract, OutputRequirement, SplitRequest,
};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::read;
use ktpu_sim::l0;
use ktpu_sim::l2::view::{Buf, Layout};
use ktpu_sim::machine::Machine;
use ktpu_sim::mem::WORD;
use ktpu_sim::num::mx7;
use ktpu_sim::Dram;
use serde_json::json;

pub(super) fn coords(card: &Card, kind: UnitKind) -> Vec<(u64, u64)> {
    let mut coords: Vec<_> = card
        .units(kind)
        .iter()
        .map(|u| (u.x() as u64, u.y() as u64))
        .collect();
    coords.sort();
    coords
}

fn execute(text: &str, card: &Card, meshes: u32, heads: usize, queries: usize) -> Vec<u8> {
    execute_seeded(text, card, meshes, heads, queries, 0x3400, None, 1)
}

pub(super) fn seed_values(
    salt: u32,
    index: usize,
    magnitude: u16,
    positive: bool,
) -> [[u16; mx7::KBLOCK]; mx7::LANES] {
    let mut x = salt ^ index as u32;
    let mut values = [[0u16; mx7::KBLOCK]; mx7::LANES];
    for value in values.iter_mut().flatten() {
        x = x.wrapping_mul(1664525).wrapping_add(1013904223);
        let sign = if positive { 0 } else { x as u16 & 0x8000 };
        *value = magnitude | ((x >> 16) as u16 & 0x03FF) | sign;
    }
    values
}

pub(super) fn execute_seeded(
    text: &str,
    card: &Card,
    meshes: u32,
    heads: usize,
    queries: usize,
    magnitude: u16,
    value_magnitude: Option<u16>,
    repeats: u32,
) -> Vec<u8> {
    let module = read(text, "flash share").expect("L1 parses");
    let name = &module.names()[0];
    let body = module.body(name, "l1").expect("motif function");
    let mut binds = BTreeMap::new();
    let mut spans = Vec::new();
    let mut at = 4096;
    for (name, ty) in &body.params {
        let buffer = Buf::of(ty, at).expect("physical buffer");
        binds.insert(name.clone(), i128::from(at));
        at += (buffer.bytes() + 4095) / 4096 * 4096;
        spans.push((name, buffer));
    }
    let mut mem = Dram::new(at as usize);
    mem.bytes.fill(0xCD);
    let mut output = 0..0;
    for (port, buffer) in &spans {
        let start = buffer.at as usize;
        let end = start + buffer.bytes() as usize;
        if *port == "y" {
            output = start..end;
        }
        if !matches!(buffer.layout, Layout::Mx7 { .. }) {
            continue;
        }
        let salt = match port.as_str() {
            "q" => 0x12345678u32,
            "k" => 0x87654321u32,
            "v" => 0x31415926u32,
            _ => unreachable!("only Q/K/V are packed"),
        };
        let (amplitude, positive) = match (port.as_str(), value_magnitude) {
            ("v", Some(value)) => (value, true),
            _ => (magnitude, false),
        };
        for (index, bytes) in mem.bytes[start..end]
            .chunks_mut(mx7::ENTRY_BYTES)
            .enumerate()
        {
            let values = seed_values(salt, index, amplitude, positive);
            for (word, target) in mx7::entry(&values)
                .write(buffer.dtype == "mx7b")
                .iter()
                .zip(bytes.chunks_mut(WORD))
            {
                target.copy_from_slice(word);
            }
        }
    }
    assert!(!output.is_empty(), "the output allocation is watched");
    mem.watch_writes(output.clone())
        .expect("watch output bytes");
    let node_programs =
        programs(&module, name, &binds, card, Some(meshes), None).expect("flash node programs");
    let builder = Builder::new(card).expect("package builder");
    let mut cores = Cores::new(card).expect("image residents");
    // ENGINE packages execute in node-program order on one mesh and shared DRAM.
    let mut machine = Machine::new(
        mem,
        card.units(UnitKind::Vc).len(),
        card.units(UnitKind::Mg).len(),
        128,
        7,
    );
    machine.isa = Some(Rc::new(Isa::reference(card).expect("machine ISA")));
    for np in &node_programs {
        let facts = ktpu_l0::Facts::of_mesh(card, np.mesh).expect("mesh facts");
        let units = l0::Units {
            vc: coords(card, UnitKind::Vc),
            mg: coords(card, UnitKind::Mg),
            src_mover: facts.src_mover(),
            counter_bits: facts.counter_bits,
        };
        for repeat in 0..repeats {
            // L1.LOW.003: deltas use the actual preceding invocation's residents.
            let opts = Opts {
                fetch: Some((card.die.fetch_port.x(), card.die.fetch_port.y())),
                cores: Some(&mut cores),
                relocate: false,
            };
            let (package, _) = builder.package(np, opts).expect("flash package");
            println!("mesh={} execution={repeat}", np.mesh);
            l0::run(&package, &units, &mut machine)
                .expect(&format!("mesh={} execution={repeat}", np.mesh));
            assert!(
                machine.mg.iter().all(|mg| mg.credits == 0),
                "each share spends its initial and returned credits before the next invocation"
            );
        }
    }
    for (word, counts) in machine
        .mem
        .write_counts()
        .expect("write census")
        .chunks(WORD)
        .enumerate()
    {
        let row = word * WORD / 128;
        let expected = repeats * u32::from(row / 1024 < heads && row % 1024 < queries);
        assert!(
            counts.iter().all(|&n| n == expected),
            "output word {word} writes: {counts:?}"
        );
    }
    machine.mem.bytes[output].to_vec()
}

#[test]
fn flash_splits_match_one_share_and_write_each_output_word_once() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    let machine = ktpu_machine::Machine::load(root.join("machines/multimesh_v9t8.json"))
        .expect("four-node machine");
    let full_request = SplitRequest {
        nodes: NodeSet::new(vec![0], &machine).expect("grant"),
        consumer: OutputRequirement::default(),
        dispatch: DispatchProfile::Common,
        numerics: NumericContract::default(),
        input_forms: vec![],
        force: Some("heads".into()),
    };
    let full = call_split("attention.flash", &json!({}), &full_request, &machine)
        .expect("full template spec");
    let expected = TEMPLATE
        .replace("q + T*8K", "q + (T/8)*64K + (T % 8)*8K")
        .replace(
            "y + (t - 5)*4K",
            "y + ((t - 5)/32)*128K + ((t - 5) % 32)*4K",
        )
        .replace("y + t*4K", "y + (t/32)*128K + (t % 32)*4K");
    let mut actual = full.l1.expect("full P=1 L1");
    // P=1 has one lexical namespace and no address or scratch displacement.
    let mut names = vec!["e", "pa", "pb", "fa", "fb", "va", "vb"];
    for line in TEMPLATE.lines() {
        if line.starts_with("image ") || line.starts_with("macro ") {
            names.push(
                line.split_whitespace()
                    .nth(1)
                    .and_then(|s| s.split('(').next())
                    .expect("template definition"),
            );
        }
    }
    for name in names {
        actual = actual.replace(&format!("{name}_0"), name);
    }
    let statements = |text: &str| -> Vec<String> {
        text.lines()
            .map(|line| line.split('#').next().expect("statement").trim())
            .filter(|line| !line.is_empty() && *line != "node 0")
            .map(str::to_string)
            .collect()
    };
    let expected = statements(&expected);
    let actual = statements(&actual);
    assert_eq!(actual.len(), expected.len(), "full P=1 statement count");
    for (index, (actual, expected)) in actual.iter().zip(&expected).enumerate() {
        assert_eq!(actual, expected, "full P=1 statement {index}");
    }
    for args in [
        json!({"heads": 0}),
        json!({"heads": 11}),
        json!({"queries": 0}),
        json!({"queries": 129}),
        json!({"queries": 1152}),
        json!({"heads": 1.5}),
        json!({"queries": null}),
        json!({"keys": 128}),
        json!([]),
    ] {
        let error = call_split("attention.flash", &args, &full_request, &machine)
            .err()
            .expect("outside the logical template contract");
        assert_eq!(error.clause, "API.OP.001");
    }
    let heads = 2;
    let queries = 256;
    let mut one_share = None;
    for grant in [vec![0], vec![1, 0], vec![3, 1, 0, 2]] {
        let p = grant.len();
        let mut options = vec!["heads".to_string(), "query-rows".to_string()];
        if p == 4 {
            options.push("heads-query-2x2".into());
        } else if p == 2 {
            options.push("heads-query-1x2".into());
        } else {
            options.truncate(1);
        }
        for id in options {
            let request = SplitRequest {
                nodes: NodeSet::new(grant.clone(), &machine).expect("grant"),
                consumer: OutputRequirement::default(),
                dispatch: DispatchProfile::Common,
                numerics: NumericContract::default(),
                input_forms: vec![],
                force: Some(id.clone()),
            };
            let plan = call_split(
                "attention.flash",
                &json!({"heads": heads, "queries": queries}),
                &request,
                &machine,
            )
            .expect("forceable flash split");
            validate_ownership(&plan.split.output).expect("physical output ownership");
            assert_eq!(plan.split.id, id);
            let output = execute(
                plan.l1.as_deref().expect("executable L1 motif"),
                &machine,
                p as u32,
                heads,
                queries,
            );
            if p == 1 {
                one_share = Some(output);
            } else {
                assert_eq!(
                    &output,
                    one_share.as_ref().expect("P=1 reference"),
                    "P={p} {id}"
                );
            }
        }
    }
}
