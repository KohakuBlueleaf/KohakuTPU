//! The planner records the contraction knobs it plans under: every product
//! of every recorded run, planned under knobs away from their defaults, names
//! them in its `matmul.plan` and `matmul.mm_kernel` calls, and each call
//! replays (`ktpu_kernels::call`) to the tiling the planner bound storages
//! for, never to the defaults'. A call takes an f16 A only where its kernel
//! does and its producer laid A out for it.

mod common;

use common::goldens;
use ktpu_kernels::gen::matmul::{self, defaults, Group, Knobs};
use ktpu_plan::{lower, run, Case, Step};
use serde_json::Value;

fn every_knob() -> Knobs {
    Knobs {
        pad_m: true,
        n_tiles: 2,
        dense_n: true,
        pad_k: true,
        batch_tiles: true,
        ring_depth: 2,
        split_vc: true,
        tail_bands: true,
        activation_fp16: true,
        quant_ring: 2,
    }
}

#[test]
fn every_product_records_and_replays_its_knobs() {
    let k = every_knob();
    assert_ne!(k, defaults());
    let (mut products, mut moved, mut f16) = (0, 0, 0);
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let m = case.machine();
        for r in &case.runs {
            let l = lower::plan(&case.plans[r.plan].uops).expect("a run's ast plans");
            let x = match run::execute_under(l, &r.state, &m, k) {
                Ok(x) | Err((_, x)) => x,
            };
            for s in &x.steps {
                let Step::Launch(l) = s else { continue };
                let Some(call) = l.call.as_ref().filter(|c| c.gen == "matmul.mm_kernel") else {
                    continue;
                };
                products += 1;
                let plan = &call.args["mm"]["made_by"]["args"];
                for (name, v) in k.args(Group::Plan) {
                    assert_eq!(plan[name], v, "{}: {name}", case.case);
                }
                for (name, v) in k.args(Group::Kernel) {
                    if name != "activation_fp16" {
                        assert_eq!(call.args[name], v, "{}: {name}", case.case);
                    }
                }
                let replayed = ktpu_kernels::call::mm_plan(plan, &m).unwrap();
                if call.args["activation_fp16"] == Value::Bool(true) {
                    assert!(
                        matmul::takes_f16(replayed.k, replayed.nk, &k, &m),
                        "{}",
                        case.case
                    );
                    f16 += 1;
                }
                let mut unrecorded = plan.clone();
                for (name, _) in k.args(Group::Plan) {
                    unrecorded.as_object_mut().unwrap().remove(name);
                }
                let bare = ktpu_kernels::call::mm_plan(&unrecorded, &m).unwrap();
                let want = matmul::plan(
                    replayed.bt,
                    replayed.m,
                    replayed.n,
                    replayed.k,
                    replayed.shared,
                    replayed.quantum,
                    ktpu_machine::PAIRING,
                    &m,
                    &k,
                )
                .unwrap();
                assert_eq!(replayed, want, "{}", case.case);
                assert_eq!(
                    call.args["out"]["rows"],
                    Value::from(want.bt * want.mp),
                    "{}: the output holds the replayed tiling's rows",
                    case.case
                );
                if bare != replayed {
                    moved += 1;
                }
            }
        }
    }
    assert!(products > 0);
    assert!(
        moved > 0,
        "no golden product's tiling moves under the knobs"
    );
    assert!(f16 > 0, "no golden product takes an f16 A under every knob");
    eprintln!("{products} products, {moved} tiled otherwise, {f16} of an f16 A under every knob");
}

/// The goldens witness the recording: with the knobs unrecorded every
/// recorded run holding a product takes other steps than its golden, first
/// in a recorded knob.
#[cfg(feature = "mutants")]
#[test]
fn knob_recording_reverse_witness() {
    let mut runs = 0;
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let m = case.machine();
        for r in &case.runs {
            let product = r.steps.iter().any(|s| {
                matches!(s, Step::Launch(l) if l.call.as_ref().is_some_and(|c| c.gen == "matmul.mm_kernel"))
            });
            if !product {
                continue;
            }
            runs += 1;
            let l = lower::plan(&case.plans[r.plan].uops).expect("a run's ast plans");
            let x = ktpu_kernels::mutants::with(matmul::UNRECORDED_MUTANT, || {
                match run::execute(l, &r.state, &m) {
                    Ok(x) | Err((_, x)) => x,
                }
            });
            let (have, want) = (common::normal(&x.steps, true), common::want(&r.steps, true));
            let at = have.iter().zip(&want).position(|(a, b)| a != b);
            let why = at.map(|i| common::first_diff(&have[i], &want[i]));
            assert!(
                why.as_ref()
                    .is_some_and(|w| matmul::KNOBS.iter().any(|k| w.contains(k.name))),
                "{}: {why:?}",
                case.case
            );
        }
    }
    assert!(runs > 0);
}
