//! Capacity admission of complete per-sysnode allocations.

use super::*;
use ktpu_kernels::split::cost::{price_bounded, select_split, ChoiceCache};
use ktpu_kernels::split::{CostEstimate, LegalityWitness, SplitAxis, SplitOption};

#[test]
fn split_selection_accounts_for_all_batch_rings() {
    let m = machine();
    let args = json!({
        "mm": {"made_by": {"gen": "matmul.plan", "args": {
            "bt": 4, "m": 197, "n": 3072, "k": 768, "shared": true,
            "quantum": 4, "waves": true, "pad_m": false, "n_tile": "auto"
        }}},
        "e": {"nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}],
              "root": 2},
        "extras": [{"kind": "col", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0}],
        "out": {"kind": "full", "dtype": "f16", "offset": 0, "pitch": 3072, "rows": 800},
        "activation_any": false, "activation_fp16": false, "ring_depth": 2,
        "quant_ring": 0, "tail_bands": true
    });
    let req = request(&m);
    let forced = |id: &str| {
        let mut forced = req.clone();
        forced.force = Some(id.into());
        call_split("matmul.mm_kernel", &args, &forced, &m)
    };
    let error = forced("p1").expect_err("all four batch rings must fit one node");
    assert_eq!(error.clause, "DSL.KERN.020");
    assert!(error.message.contains("staging capacity:"), "{error}");
    let chosen = call_split("matmul.mm_kernel", &args, &req, &m).expect("fitting option");
    let module = if let Some(text) = &chosen.l1 {
        ktpu_l1::text::read(text, "selected L1").expect("hand schedule")
    } else {
        let program = ktpu_graph::program(&chosen.kernel.graph, &m).expect("selected graph");
        ktpu_l2::compile_l1(
            &program,
            &m,
            &ktpu_l2::PASSES,
            true,
            &ktpu_l2::Search::default(),
        )
        .expect("selected schedule")
        .l1
    };
    let callers = chosen
        .kernel
        .graph
        .ports
        .iter()
        .map(|p| p.name.clone())
        .collect();
    let drains = ktpu_l1::lower::node::plan_drains(&module, &chosen.kernel.graph.name, &m, None)
        .expect("drain layout");
    let arenas = ktpu_l1::layout::scratch::arenas_with_bursts(
        &drains.module,
        &chosen.kernel.graph.name,
        &callers,
        &m,
        &drains.burst_ports,
    )
    .expect("runtime allocations");
    let mut windows = BTreeMap::<i128, u64>::new();
    for arena in arenas.iter().filter(|a| a.space == "stage") {
        *windows.entry(arena.node.unwrap_or(0)).or_default() += arena.bytes.div_ceil(256) * 256;
    }
    assert!(!windows.is_empty(), "the ring optimization remains enabled");
    assert!(windows
        .values()
        .all(|&bytes| bytes <= m.die.stage_bytes().expect("physical capacity")));

    // A refused low-bound candidate cannot prune the next priced candidate.
    let mut naive = chosen.split.clone();
    naive.id = "p1".into();
    let options = price_bounded(
        vec![(naive, Some(0)), (chosen.split.clone(), Some(1))],
        |option| {
            if option.id == "p1" {
                Err(error.clone())
            } else {
                Ok(option)
            }
        },
    )
    .expect("capacity refusal leaves fitting candidates");
    let next = select_split(options, None).expect("next priced option");
    assert_eq!(next.id, chosen.split.id);
    assert!(next
        .legality
        .clauses
        .iter()
        .any(|reason| { reason.contains("refused p1:") && reason.contains("staging capacity:") }));
}

#[test]
fn lazy_admission_checks_only_price_winners_and_reopens_bounds() {
    let option = |id: &str, cycles| SplitOption {
        id: id.into(),
        axis: SplitAxis::Unsplit,
        p: 1,
        active_nodes: 1,
        shards: vec![],
        output: OutputLayout::default(),
        events: vec![],
        conversions: 0,
        cost: CostEstimate {
            makespan: cycles,
            ..Default::default()
        },
        legality: LegalityWitness {
            numerics: NumericContract::default(),
            clauses: vec![],
        },
    };
    let candidates = vec![
        (option("loser", 30), Some(0)),
        (option("over", 5), Some(0)),
        (option("fit", 10), Some(6)),
        (option("tie", 10), Some(6)),
    ];
    let cache = ChoiceCache::default();
    let mut priced = Vec::new();
    let mut checked = Vec::new();
    let capacity = |option: &SplitOption| {
        if option.id == "fit" {
            Ok(())
        } else {
            Err(ktpu_machine::Refusal::new(
                "DSL.KERN.020",
                "staging capacity: sysnode 0 needs 512 bytes, budget 256",
            ))
        }
    };
    let chosen = cache
        .choose_bounded(
            "request".into(),
            None,
            || Ok(candidates.clone()),
            |option| {
                priced.push(option.id.clone());
                Ok(option)
            },
            |option| {
                checked.push(option.id.clone());
                capacity(option)
            },
        )
        .expect("the refused provisional bound releases deferred candidates");
    assert_eq!(chosen.id, "fit");
    assert_eq!(checked, ["over", "fit"]);
    assert_eq!(priced, ["loser", "over", "fit", "tie"]);
    assert!(chosen
        .legality
        .clauses
        .iter()
        .any(|reason| reason.contains("refused over:")));

    // A cached losing price is not an admission proof, even in an exact tie.
    let error = cache
        .choose_bounded(
            "request".into(),
            Some("tie"),
            || Ok(candidates.clone()),
            |_| panic!("an exact cached price must not be recomputed"),
            |option| {
                checked.push(option.id.clone());
                capacity(option)
            },
        )
        .expect_err("forcing an unchecked price must admit that candidate");
    assert!(error.message.contains("staging capacity:"));
    assert_eq!(checked, ["over", "fit", "tie"]);
    let reused = cache
        .choose_bounded(
            "request".into(),
            Some("fit"),
            || panic!("a fitting forced winner retains its cached proof"),
            |_| panic!("a fitting forced winner retains its price"),
            |_| panic!("a fitting forced winner retains its admission"),
        )
        .expect("cached fitting winner");
    assert_eq!(reused.id, chosen.id);
}
