//! A missing fill/unpack edge is safe at even corners and racy for odd trips.

use ktpu::templates::{proof, verification};
use ktpu_isa::InstSet;
use ktpu_l0::format::template::{Bounds, ParameterTable, Template, ValuePatch};
use ktpu_l0::{Op, Package, Step, Unit};
use ktpu_machine::Machine;
use ktpu_verify::{check, Kind, Nodes, Target};

fn target() -> Target {
    Target::from_machine(&Machine::named("multimesh_v9t6").unwrap(), Some(1)).unwrap()
}

fn enc(set: &InstSet, name: &str, vals: &[(&str, i64)]) -> ktpu_l0::Word {
    ktpu_l0::Word(set.by_name(name).unwrap().encode(vals).unwrap().0)
}

fn fixture(fixed: bool) -> Template {
    let target = target();
    let half = target.facts.vc_l1_words / 2;
    let core =
        |name, vals: &[(&str, i64)]| enc(&target.isa.vector.core.set, name, vals).0[0] as i64;
    let mut words = vec![
        core("UPTR", &[("ptr", 0)]),
        core("UINC", &[("ptr", half)]),
        core("VSETI", &[("sreg", 15)]),
        2,
        core("VLOOP", &[("sreg", 15), ("body", 1)]),
        core("VUNPK", &[("reg", 0), ("n1", 0), ("off", 0), ("rel", 1)]),
        core("VSYNC", &[("waiter", 0), ("on", 1)]),
        core("VFILL", &[("ad", 0), ("off", half)]),
    ];
    if fixed {
        words.push(core("VSYNC", &[("waiter", 1), ("on", 0)]));
    }
    words.push(core(
        "VUNPK",
        &[("reg", 0), ("n1", 0), ("off", 0), ("rel", 1)],
    ));
    words.push(core("VHALT", &[]));
    let mut payloads: Vec<_> = words
        .iter()
        .enumerate()
        .map(|(i, &word)| {
            enc(
                &target.isa.vector.set,
                "IMEM",
                &[("addr", i as i64), ("word", word)],
            )
        })
        .collect();
    payloads.push(enc(&target.isa.vector.set, "RUN", &[("pc", 0)]));
    let (bit, width) = target.isa.vector.set.by_name("IMEM").unwrap().span("word");
    Template {
        package: Package {
            units: vec![Unit {
                type_code: 0x5643,
                x: 2,
                y: 1,
                mesh: 0,
                credit: 512,
                fetch: 0,
            }],
            steps: vec![Step::new(Op::Dispatch, 0, payloads.len() as u64, 0, 0)],
            payloads,
            ..Default::default()
        },
        table: ParameterTable {
            environment: None,
            buffer_sizes: None,
            parameters: [("trips".into(), Bounds { lo: 2, hi: 4 })].into(),
            sites: vec![ValuePatch {
                name: "core_trip_count".into(),
                payload: 3,
                step: None,
                bit,
                width,
                shift: 0,
                constant: 0,
                coefficients: [("trips".into(), 1)].into(),
                buffer: None,
            }],
        },
    }
}

#[test]
fn xb_patch_confinement_and_opcode_mutation() {
    let target = target();
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let word = enc(&target.isa.vector.core.set, "XB", &[("xk", 0)]).0[0];
    let mut t = fixture(true);
    t.package.payloads = vec![
        enc(
            &target.isa.vector.set,
            "IMEM",
            &[("addr", 0), ("word", word as i64)],
        ),
        enc(&target.isa.vector.set, "RUN", &[("pc", 0)]),
    ];
    t.package.steps = vec![Step::new(Op::Dispatch, 0, 2, 0, 0)];
    let (bit, width) = target.isa.vector.core.set.by_name("XB").unwrap().span("xk");
    t.table.parameters = [("rotation".into(), Bounds { lo: 0, hi: 15 })].into();
    let p = &mut t.table.sites[0];
    p.payload = 0;
    p.bit = bit;
    p.width = width;
    p.coefficients = [("rotation".into(), 1)].into();
    assert!(
        ktpu::templates::confinement::derive(&t, &machine)
            .unwrap()
            .data_only["rotation"]
    );
    // A second parameter can change the CU address: no scalar-data exemption.
    let mut bad = t.clone();
    let mut address = bad.table.sites[0].clone();
    (address.bit, address.width) = target.isa.vector.imem.span("addr");
    bad.table.sites.push(address);
    assert!(
        !ktpu::templates::confinement::derive(&bad, &machine)
            .unwrap()
            .data_only["rotation"]
    );
    // The very same XB-looking word is control data after VSETI.
    let seti = enc(&target.isa.vector.core.set, "VSETI", &[("sreg", 15)]).0[0];
    t.package.payloads[0] = enc(
        &target.isa.vector.set,
        "IMEM",
        &[("addr", 1), ("word", word as i64)],
    );
    t.package.payloads.insert(
        0,
        enc(
            &target.isa.vector.set,
            "IMEM",
            &[("addr", 0), ("word", seti as i64)],
        ),
    );
    t.package.steps[0].count = 3;
    t.table.sites[0].payload = 1;
    assert!(
        !ktpu::templates::confinement::derive(&t, &machine)
            .unwrap()
            .data_only["rotation"]
    );
}

#[test]
fn loop_immediates_cannot_be_confined_as_data() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let t = fixture(false);
    let certificate = ktpu::templates::confinement::derive(&t, &machine).unwrap();
    assert!(!certificate.data_only["trips"]);
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with("verify.confinement/loop-data", || {
        let bad = ktpu::templates::confinement::derive(&t, &machine).unwrap();
        assert_ne!(certificate, bad);
        assert!(bad.data_only["trips"]);
        let target = target();
        let corners = t.bind(&[], &[("trips".into(), 2)].into()).unwrap();
        assert!(check(&[[(0, corners)].into()], &target).is_empty());
        let interior = t.bind(&[], &[("trips".into(), 3)].into()).unwrap();
        assert!(!check(&[[(0, interior)].into()], &target).is_empty());
    });
}

#[test]
fn corners_miss_real_odd_trip_race_adjacent_catches_it() {
    let target = target();
    let mutant = fixture(false);
    for trips in [2, 4] {
        let bound = mutant.bind(&[], &[("trips".into(), trips)].into()).unwrap();
        let nodes = Nodes::from([(0, bound)]);
        assert!(check(&[nodes.clone(), nodes], &target).is_empty());
    }
    let bound = mutant.bind(&[], &[("trips".into(), 3)].into()).unwrap();
    let nodes = Nodes::from([(0, bound)]);
    let found = check(&[nodes.clone(), nodes], &target);
    assert!(!found.is_empty());
    assert!(found.iter().all(|f| f.kind == Kind::Race), "{found:?}");
    let bindings = [(0, vec![])].into();
    let error = verification::verify(
        &[(0, mutant)].into(),
        &bindings,
        "odd-trip",
        &target,
        16,
        1000,
    )
    .unwrap_err();
    assert!(error.contains("trips\": 3"), "{error}");
    assert!(error.contains("Race"), "{error}");
    let points = verification::verify(
        &[(0, fixture(true))].into(),
        &bindings,
        "odd-trip",
        &target,
        16,
        1000,
    )
    .unwrap();
    assert_eq!(points.len(), 3);
}

#[test]
fn whole_box_refuses_odd_race_and_residue_deletion_is_killed() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let bindings = [(0, vec![])].into();
    let templates = [(0, fixture(false))].into();
    let refused = proof::prove(&templates, &bindings, &machine, 100).unwrap_err();
    assert_eq!(refused.counterexample.as_ref().unwrap()["trips"], 3);
    assert!(refused.reason.contains("Race"), "{refused:?}");
    // No seeded or adjacent samples are passed to the proof procedure.
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with(proof::NO_RESIDUES, || {
        assert!(proof::prove(&templates, &bindings, &machine, 100).is_ok());
    });
    let safe = proof::prove(&[(0, fixture(true))].into(), &bindings, &machine, 100).unwrap();
    assert_eq!(safe.certificate().checker_bindings, 3);
    assert_eq!(
        safe.certificate().residues[0].representatives,
        vec![2, 3, 4]
    );
    assert!(safe.bind(&[("trips".into(), 3)].into()).is_ok());
    assert!(safe.bind(&[("trips".into(), 5)].into()).is_err());
    assert!(proof::prove(&templates, &bindings, &machine, 2).is_err());
}

#[test]
fn proof_refuses_model_gaps_and_parameter_valued_sync() {
    let target = target();
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let mut t = fixture(true);
    *t.package.payloads.last_mut().unwrap() = enc(&target.isa.vector.set, "RUN", &[("pc", 500)]);
    let p = [("trips".into(), 2)].into();
    let nodes = [(0, t.bind(&[], &p).unwrap())].into();
    let error = ktpu_verify::gate(&[nodes], &target, ktpu_verify::Mode::Refuse, None).unwrap_err();
    assert!(
        matches!(error, ktpu_verify::GateError::Refused(ref findings) if findings.iter().any(|f| f.kind == ktpu_verify::Kind::Vector))
    );
    let refusal = proof::prove(&[(0, t)].into(), &[(0, vec![])].into(), &machine, 10).unwrap_err();
    assert_eq!(refusal.property, "scheduling");
    assert!(refusal.reason.contains("L1.VER.016"), "{refusal:?}");
    let mut t = fixture(true);
    t.table.parameters = [("edge".into(), Bounds { lo: 0, hi: 1 })].into();
    let (bit, width) = target
        .isa
        .vector
        .core
        .set
        .by_name("VSYNC")
        .unwrap()
        .span("on");
    t.table.sites = vec![ValuePatch {
        name: "edge".into(),
        payload: 6,
        step: None,
        bit,
        width,
        shift: 0,
        constant: 0,
        coefficients: [("edge".into(), 1)].into(),
        buffer: None,
    }];
    let refusal = proof::prove(&[(0, t)].into(), &[(0, vec![])].into(), &machine, 10).unwrap_err();
    assert!(refusal.reason.contains("synchronisation"), "{refusal:?}");
}

#[test]
fn xb_quotient_proves_all_values_with_one_checker_binding() {
    let target = target();
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let mut t = fixture(true);
    let word = enc(&target.isa.vector.core.set, "XB", &[("xk", 0)]).0[0];
    t.package.payloads[1] = enc(
        &target.isa.vector.set,
        "IMEM",
        &[("addr", 1), ("word", word as i64)],
    );
    let (bit, width) = target.isa.vector.core.set.by_name("XB").unwrap().span("xk");
    let hi = ((1u64 << width) - 1) as i64;
    t.table.parameters = [
        ("rotation".into(), Bounds { lo: 0, hi }),
        (
            "unused".into(),
            Bounds {
                lo: i64::MIN,
                hi: i64::MAX,
            },
        ),
    ]
    .into();
    t.table.sites = vec![ValuePatch {
        name: "rotation".into(),
        payload: 1,
        step: None,
        bit,
        width,
        shift: 0,
        constant: 0,
        coefficients: [("rotation".into(), 1)].into(),
        buffer: None,
    }];
    let proven =
        proof::prove(&[(0, t.clone())].into(), &[(0, vec![])].into(), &machine, 1).unwrap();
    assert_eq!(proven.certificate().checker_bindings, 1);
    assert_eq!(proven.certificate().eliminated, vec!["rotation", "unused"]);
    for rotation in [0, hi / 2, hi] {
        let p = [("rotation".into(), rotation), ("unused".into(), i64::MAX)].into();
        let nodes = proven.bind(&p).unwrap();
        assert_eq!(nodes[&0], t.bind(&[], &p).unwrap());
        assert!(check(&[nodes.clone(), nodes], &target).is_empty());
    }
    let mut repeated = t.clone();
    repeated.package.steps[0].op = Op::Repeat;
    repeated.package.steps[0].arg = 2u64 << 32;
    assert!(proof::prove(
        &[(0, repeated.clone())].into(),
        &[(0, vec![])].into(),
        &machine,
        1
    )
    .is_err());
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with("verify.template/repeat-data", || {
        assert!(proof::prove(&[(0, repeated)].into(), &[(0, vec![])].into(), &machine, 1).is_ok());
    });
    t.table.parameters.get_mut("rotation").unwrap().hi = hi + 1;
    assert!(proof::prove(&[(0, t)].into(), &[(0, vec![])].into(), &machine, 1).is_err());
}

#[test]
fn two_parameter_affine_overlap_only_in_the_interior() {
    let target = target();
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let mut t = fixture(false);
    let word = enc(
        &target.isa.vector.core.set,
        "VFILL",
        &[("ad", 0), ("off", 64)],
    )
    .0[0];
    t.package.payloads[7] = enc(
        &target.isa.vector.set,
        "IMEM",
        &[("addr", 7), ("word", word as i64)],
    );
    let word = enc(
        &target.isa.vector.core.set,
        "VUNPK",
        &[("reg", 0), ("n1", 0), ("off", 128), ("rel", 0)],
    )
    .0[0];
    t.package.payloads[8] = enc(
        &target.isa.vector.set,
        "IMEM",
        &[("addr", 8), ("word", word as i64)],
    );
    let (bit, width) = target
        .isa
        .vector
        .core
        .set
        .by_name("VFILL")
        .unwrap()
        .span("off");
    t.table.parameters = [
        ("x".into(), Bounds { lo: 0, hi: 2 }),
        ("y".into(), Bounds { lo: 0, hi: 2 }),
    ]
    .into();
    t.table.sites = vec![ValuePatch {
        name: "affine_fill".into(),
        payload: 7,
        step: None,
        bit,
        width,
        shift: 0,
        constant: 64,
        coefficients: [("x".into(), 16), ("y".into(), 48)].into(),
        buffer: None,
    }];
    for x in 0..=2 {
        for y in 0..=2 {
            let nodes: Nodes = [(
                0,
                t.bind(&[], &[("x".into(), x), ("y".into(), y)].into())
                    .unwrap(),
            )]
            .into();
            let findings = check(&[nodes.clone(), nodes], &target);
            assert_eq!(
                findings.is_empty(),
                (x, y) != (1, 1),
                "{x},{y}: {findings:?}"
            );
        }
    }
    let refusal = proof::prove(&[(0, t)].into(), &[(0, vec![])].into(), &machine, 100).unwrap_err();
    let p = refusal.counterexample.as_ref().unwrap();
    assert_eq!(p, &[("x".into(), 1), ("y".into(), 1)].into(), "{refusal:?}");
    assert!(refusal.reason.contains("Race"), "{refusal:?}");
}
