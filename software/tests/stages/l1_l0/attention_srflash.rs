//! The SR fixture lowers and encodes SR streams; the value simulator has no
//! SR execution model. Check stream preservation, credits and destination walks.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use ktpu_kernels::call_split;
use ktpu_kernels::gen::attention::srflash::TEMPLATE;
use ktpu_kernels::split::{
    DispatchProfile, NodeSet, NumericContract, OutputRequirement, SplitRequest,
};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::text::{read, Env, Expander, Term, Val};
use ktpu_machine::Machine;
use ktpu_sim::l2::view::Buf;
use serde_json::json;

fn packages(text: &str, machine: &Machine, meshes: u32) -> Vec<Vec<u8>> {
    let module = read(text, "SR motif").expect("L1");
    let name = &module.names()[0];
    let body = module.body(name, "l1").expect("body");
    let mut binds = BTreeMap::new();
    let mut at = 4096;
    for (name, ty) in &body.params {
        let buffer = Buf::of(ty, at).expect("buffer");
        binds.insert(name.clone(), i128::from(at));
        at += (buffer.bytes() + 4095) / 4096 * 4096;
    }
    let node_programs =
        programs(&module, name, &binds, machine, Some(meshes), None).expect("SR programs");
    let builder = Builder::new(machine).expect("builder");
    node_programs
        .iter()
        .map(|np| {
            let opts = Opts {
                fetch: Some((machine.die.fetch_port.x(), machine.die.fetch_port.y())),
                cores: None,
                relocate: false,
            };
            let (package, _) = builder.package(np, opts).expect("SR package");
            let facts = ktpu_l0::Facts::of_mesh(machine, np.mesh).expect("mesh");
            package.to_bytes(&facts).expect("encoded SR stream")
        })
        .collect()
}

fn sr_index(term: &Term) -> Option<usize> {
    match term {
        Term::View(name, args) if name == "sr" => Some(args[0].as_int().expect("SR role") as usize),
        _ => None,
    }
}

fn check_walks_and_credits(text: &str, heads: i128, queries: i128) {
    let module = read(text, "SR shares").expect("L1");
    let body = module.body("srflash", "l1").expect("body");
    let mut env = Env::default();
    for (name, _) in &body.params {
        env.insert(name.clone(), Val::Int(0));
    }
    let (reader, macros) = (module.reader(), module.macro_table());
    let mut writes = BTreeSet::new();
    for node in &body.stmts {
        let flat = Expander::new(&macros, &reader, true)
            .stmts(&node.body, &env)
            .expect("instantiated SR streams");
        let mut credits = [0i128; 2];
        let mut norm_rows = [None; 2];
        for send in flat.iter().filter(|s| s.op == "send") {
            let role = send.positional().first().and_then(|t| sr_index(t));
            for instruction in &send.body {
                if instruction.op == "post" {
                    let sr = sr_index(instruction.kw("to").expect("credit receiver"))
                        .expect("SR credits");
                    credits[sr] += instruction.kw("n").and_then(Term::as_int).expect("credits");
                }
                if let Some(sr) = role {
                    if instruction.op == "job" {
                        norm_rows[sr] =
                            if instruction.kw("out").and_then(Term::as_name) == Some("f16") {
                                Some(
                                    instruction
                                        .kw("nsr")
                                        .and_then(Term::as_int)
                                        .expect("subrows"),
                                )
                            } else {
                                None
                            };
                    }
                    if instruction.op == "run" {
                        if let Some(rows) = norm_rows[sr] {
                            let start = instruction
                                .kw("out")
                                .and_then(Term::as_int)
                                .expect("Y origin");
                            let stride = instruction
                                .kw("ostride")
                                .and_then(Term::as_int)
                                .expect("Y stride");
                            for row in 0..rows {
                                for word in 0..8 {
                                    assert!(
                                        writes.insert(start + (row * stride + word) * 32),
                                        "duplicate Y word"
                                    );
                                }
                            }
                        } else {
                            credits[sr] -= 1024;
                            assert!(credits[sr] >= 0, "P consumes an advertised MG bank");
                        }
                    }
                }
            }
        }
        assert_eq!(
            credits,
            [0, 0],
            "all initial/returned bank credits are spent"
        );
    }
    let expected: BTreeSet<_> = (0..heads)
        .flat_map(|head| (0..queries * 4).map(move |word| head * 131072 + word * 32))
        .collect();
    assert_eq!(
        writes, expected,
        "normalization covers each global query word once"
    );
}

#[test]
fn sr_adapter_preserves_streams_owns_global_groups_and_refuses_non_sr() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    for board in ["sru_v9t7", "multimesh_v9t8"] {
        let machine = Machine::load(root.join(format!("machines/{board}.json"))).expect("fixture");
        let mut request = SplitRequest {
            nodes: NodeSet::new(vec![0], &machine).expect("grant"),
            consumer: OutputRequirement::default(),
            dispatch: DispatchProfile::Common,
            numerics: NumericContract::default(),
            input_forms: vec![],
            force: Some("heads".into()),
        };
        let result = call_split("attention.srflash", &json!({}), &request, &machine);
        if board == "sru_v9t7" {
            let plan = result.expect("SR template");
            assert_eq!(
                packages(plan.l1.as_deref().expect("SR L1"), &machine, 1),
                packages(TEMPLATE, &machine, 1),
                "same head-pair program, including credits and row-state reuse"
            );
            request.nodes = NodeSet::new(vec![3, 1, 0, 2], &machine).expect("grant");
            request.force = Some("balanced".into());
            let plan = call_split(
                "attention.srflash",
                &json!({"heads": 3, "queries": 768}),
                &request,
                &machine,
            )
            .expect("short global SR shares");
            let text = plan.l1.as_deref().expect("SR L1");
            check_walks_and_credits(text, 3, 768);
            packages(text, &machine, 4);
        } else {
            let error = result
                .err()
                .expect("SR capacity refusal, not another attention motif");
            assert_eq!(error.clause, "DSL.KERN.020");
            assert!(error.message.contains("two distinct SR roles"), "{error}");
        }
    }
}
