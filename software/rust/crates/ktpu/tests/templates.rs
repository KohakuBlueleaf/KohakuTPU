//! Tier-0 differential fit, structure and independent byte-proof witnesses.
#![cfg(feature = "stack")]

use ktpu::templates::{derive, fit, points, prove, structure, Class, Sample};
use ktpu_l0::format::template::{Bounds, Params};
use ktpu_l0::format::{Op, Step, Unit};
use ktpu_l0::word::{Word, WordOps};
use ktpu_l0::{Facts, Package};
use ktpu_machine::Machine;

#[test]
fn mover_stride_is_one_field_across_word_lane_boundary() {
    let class: Class = [("pitch_words".into(), Bounds { lo: 1, hi: 256 })].into();
    let samples: Vec<_> = points(&class, 100)
        .unwrap()
        .into_iter()
        .map(|params| {
            let mut payload = Word::ZERO;
            payload.set(0, 64, ktpu_l0::mover::R_DIM);
            payload.set(64, 64, 16 | ((params["pitch_words"] as u64 * 32) << 20));
            Sample {
                label: "mover stride".into(),
                params,
                package: Package {
                    steps: vec![Step::new(Op::Mover, 0, 1, 0, 0)],
                    payloads: vec![payload],
                    ..Package::default()
                },
            }
        })
        .collect();
    let template = derive(class, &samples).unwrap();
    assert_eq!(template.table.sites[0].bit, 84);
    assert_eq!(template.table.sites[0].width, 32);
    let facts = Facts::of(&Machine::named("multimesh_v9t6").unwrap());
    for sample in &samples {
        prove(&template, sample, &[], &facts).unwrap();
    }
    let mut mutant = template.clone();
    mutant.table.sites[0].width = 12;
    assert!(prove(&mutant, samples.last().unwrap(), &[], &facts).is_err());
}

fn class() -> Class {
    [
        ("x".into(), Bounds { lo: 2, hi: 6 }),
        ("y".into(), Bounds { lo: 3, hi: 7 }),
    ]
    .into()
}

fn samples() -> Vec<Sample> {
    points(&class(), 100)
        .unwrap()
        .into_iter()
        .map(|params| {
            let v = (11 + 2 * params["x"] + 5 * params["y"]) as u64;
            Sample {
                label: format!("point {params:?}"),
                params,
                package: Package {
                    steps: vec![Step::bare(Op::End)],
                    payloads: vec![Word::from_u64(v)],
                    ..Package::default()
                },
            }
        })
        .collect()
}

#[test]
fn derived_counts_keep_shape_witnesses_and_byte_proof() {
    use ktpu::templates::{derive_with_values, values};
    let class: Class = [("R".into(), Bounds { lo: 1, hi: 65 })].into();
    let recipes: values::Recipes = serde_json::from_str(r#"{"trips":{"op":"div","a":{"op":"add","a":{"op":"param","name":"R"},"b":{"op":"const","value":15}},"divisor":16}}"#).unwrap();
    let ss: Vec<_> = points(&class, 100)
        .unwrap()
        .into_iter()
        .map(|params| {
            let count = (params["R"] + 15) / 16;
            Sample {
                label: format!("{params:?}"),
                params,
                package: Package {
                    payloads: vec![Word::from_u64((count * 7) as u64)],
                    steps: vec![Step::bare(Op::End)],
                    ..Package::default()
                },
            }
        })
        .collect();
    assert!(derive(class.clone(), &ss).is_err());
    let template = derive_with_values(class.clone(), &ss, &recipes).unwrap();
    assert!(derive_with_values(class, &ss[1..], &recipes)
        .unwrap_err()
        .contains("missing verification point"));
    let facts = Facts::of(&Machine::named("multimesh_v9t6").unwrap());
    for r in 1..=65 {
        let params = values::bind(&recipes, &[("R".into(), r)].into()).unwrap();
        let mut direct = ss[0].clone();
        direct.params = params;
        direct.package.payloads[0] = Word::from_u64((((r + 15) / 16) * 7) as u64);
        prove(&template, &direct, &[], &facts).unwrap();
        direct.package.payloads[0] = Word::from_u64((r / 16 * 7) as u64);
        if r % 16 != 0 {
            assert!(prove(&template, &direct, &[], &facts).is_err());
        }
    }
}

#[test]
fn exact_multivariate_fit() {
    let rows = samples()
        .iter()
        .map(|s| (s.params.clone(), s.package.payloads[0].get(0, 32) as i128))
        .collect::<Vec<_>>();
    let (c, coefficients) = fit(&class(), &rows).unwrap();
    assert_eq!(c, 11);
    assert_eq!(
        coefficients,
        Params::from([("x".into(), 2), ("y".into(), 5)])
    );
}

#[test]
fn rejects_cross_term_and_interior_only_mutant() {
    let mut rows: Vec<_> = samples()
        .iter()
        .map(|s| (s.params.clone(), (s.params["x"] * s.params["y"]) as i128))
        .collect();
    assert!(fit(&class(), &rows).unwrap_err().contains("non-affine"));
    for (p, v) in &mut rows {
        *v = (11 + 2 * p["x"] + 5 * p["y"]) as i128;
    }
    let interior = rows
        .iter_mut()
        .find(|(p, _)| p["x"] == 4 && p["y"] == 5)
        .unwrap();
    interior.1 += 1;
    assert!(fit(&class(), &rows).unwrap_err().contains("non-affine"));
}

#[test]
fn structure_mutants_refused() {
    let a = samples()[0].package.clone();
    let mut b = a.clone();
    b.payloads.push(Word::ZERO);
    assert_eq!(structure(&a, &b), Err("payloads.len".into()));
    b = a.clone();
    b.steps[0].count = 3;
    assert_eq!(structure(&a, &b), Err("steps".into()));
    let mut ss = samples();
    ss[1].package = b;
    assert!(derive(class(), &ss).unwrap_err().contains("structure:"));
}

#[test]
fn dropped_term_fails_independent_wire_proof() {
    let facts = Facts::of(&Machine::named("multimesh_v9t6").unwrap());
    let ss = samples();
    let template = derive(class(), &ss).unwrap();
    for s in &ss {
        prove(&template, s, &[], &facts).unwrap();
    }
    let mut mutant = template.clone();
    mutant.table.sites[0].coefficients.remove("y");
    let err = prove(&mutant, &ss[0], &[], &facts).unwrap_err();
    assert!(err.contains("payloads[0]"), "{err}");
    assert!(err.contains("wire byte"), "{err}");
}

#[test]
fn rejects_missing_witness_and_bounds() {
    let mut ss = samples();
    ss.pop();
    assert!(derive(class(), &ss)
        .unwrap_err()
        .contains("missing verification point"));
    let bad = Class::from([("x".into(), Bounds { lo: 7, hi: 2 })]);
    assert!(points(&bad, 100).is_err());
    assert!(points(&class(), 2).is_err());
    assert!(fit(&class(), &[]).is_err());
}

#[test]
fn negative_coefficients_are_exact() {
    let rows = samples()
        .iter()
        .map(|s| {
            (
                s.params.clone(),
                (100 - 3 * s.params["x"] + s.params["y"]) as i128,
            )
        })
        .collect::<Vec<_>>();
    let (constant, coefficients) = fit(&class(), &rows).unwrap();
    assert_eq!(constant, 100);
    assert_eq!(coefficients["x"], -3);
}

#[test]
fn constant_payload_still_requires_full_rank() {
    let mut ss = samples();
    for s in &mut ss {
        s.package.payloads[0].set(0, 32, 9);
    }
    assert!(derive(class(), &ss).unwrap().table.sites.is_empty());
    assert!(derive(class(), &ss[..1]).is_err());
}

#[test]
fn step_values_fit_and_prove_without_admitting_topology_changes() {
    let facts = Facts::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut ss = samples();
    for s in &mut ss {
        s.package.units.push(Unit {
            type_code: 0x5643,
            x: 0,
            y: 0,
            mesh: 0,
            credit: 512,
            fetch: 0x10000,
        });
        s.package.steps.insert(
            0,
            Step::new(
                Op::Repeat,
                0,
                1,
                ((2 * s.params["x"] + s.params["y"]) as u64) << 32,
                0,
            ),
        );
        s.package
            .steps
            .insert(1, Step::new(Op::Fetch, 0, s.params["x"] as u64, 0x1000, 0));
    }
    let template = derive(class(), &ss).unwrap();
    assert_eq!(
        template
            .table
            .sites
            .iter()
            .filter(|s| s.step.is_some())
            .count(),
        2
    );
    for s in &ss {
        prove(&template, s, &[], &facts).unwrap();
    }
    let mut mutant = template.clone();
    mutant.table.sites.retain(|s| s.step.is_none());
    assert!(ss.iter().any(|s| prove(&mutant, s, &[], &facts).is_err()));
    for (index, change) in [(0, "first"), (0, "increments"), (1, "route")] {
        let mut bad = ss.clone();
        let step = &mut bad[1].package.steps[index];
        match change {
            "first" => step.arg += 1,
            "increments" => step.arg += 1 << 48,
            _ => step.unit += 1,
        }
        assert!(derive(class(), &bad).unwrap_err().contains("structure:"));
    }
}

#[test]
fn frozen_search_reuses_choices_and_refuses_new_math() {
    let source = "target ktpu.v9\n\nfn add.l2(x: f16[512, 128] @dram, y: f16[512, 128] @dram, out: f16[512, 128] @dram)\n    par c in 0..2 on vc[c]\n        for t in 0..32\n            xl = load x[c*256 + t*8 : +8, :] : f16[8, 128] @l1\n            yl = load y[c*256 + t*8 : +8, :] : f16[8, 128] @l1\n            a = unpack xl : f32[8, 128] @reg\n            b = unpack yl : f32[8, 128] @reg\n            s = add a, b : f32[8, 128] @reg\n            sl = pack s : f16[8, 128] @l1\n            store out[c*256 + t*8 : +8, :] <- sl\n";
    let m = Machine::named("multimesh_v9t6").unwrap();
    let compiler = ktpu::templates::compiler::PinnedCompiler::new(source, &m).unwrap();
    let timed = compiler.search.timed();
    assert!(timed > 0);
    assert!(compiler.search.retile_count() > 0);
    let repeated = compiler.compile(source).unwrap();
    assert_eq!(compiler.search.timed(), timed);
    assert_eq!(
        compiler.representative.packages[&0].bytes,
        repeated.packages[&0].bytes
    );
    let different = source.replace("s = add", "s = mul");
    let error = compiler.compile(&different).unwrap_err();
    assert_eq!(error.refusal.clause, "L2.TEMPLATE.CHOICE");
    // The no-freeze mutant admits and searches the changed expression.
    let mutant = ktpu::compile(
        &ktpu::CardStack::default(),
        &ktpu::Input::L2(different),
        None,
        &m,
        &ktpu::Cache::off(),
    );
    assert!(mutant.is_ok());
}
