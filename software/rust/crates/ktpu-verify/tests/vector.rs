//! Device-free vector image refusal and real encoder-corruption witnesses.

use std::collections::BTreeMap;
#[cfg(feature = "mutants")]
use std::path::Path;

use ktpu_isa::unit::vector::{dim, VecCu};
#[cfg(feature = "mutants")]
use ktpu_isa::Isa;
use ktpu_isa::W256;
use ktpu_l0::{Op, Package, Step, Unit, Word};
use ktpu_machine::Machine;
use ktpu_verify::{check, gate, Held, Kind, Mode, Nodes, Target};

fn package(m: &Machine, words: &[W256]) -> Package {
    let c = &m.die.units.vc[0];
    Package {
        units: vec![Unit {
            type_code: 0x5643,
            x: c.x() as u64,
            y: c.y() as u64,
            mesh: 0,
            credit: m.unit.inst_depth as u64,
            fetch: 0,
        }],
        steps: vec![Step::new(Op::Dispatch, 0, words.len() as u64, 0, 0)],
        payloads: words.iter().map(|w| Word(w.0)).collect(),
        ..Default::default()
    }
}
fn core(t: &Target, name: &str, args: &[(&str, i64)]) -> u64 {
    t.isa
        .vector
        .core
        .set
        .by_name(name)
        .unwrap()
        .encode(args)
        .unwrap()
        .0[0]
}
fn runs(m: &Machine, t: &Target, words: &[u64], descs: &[(u32, u32, u64)]) -> Vec<Nodes> {
    let isa = &t.isa.vector;
    let mut cu = isa.program(words, 0).unwrap();
    let run = cu.pop().unwrap();
    cu.extend(
        descs
            .iter()
            .map(|&(ad, fld, value)| VecCu::Desc { ad, fld, value }.encode(isa).unwrap()),
    );
    cu.push(run);
    vec![Nodes::from([(0, package(m, &cu))])]
}
fn refused(m: &Machine, t: &Target, words: &[u64], descs: &[(u32, u32, u64)], text: &str) {
    let r = runs(m, t, words, descs);
    let f = check(&r, t);
    assert!(
        f.iter()
            .any(|f| f.kind == Kind::Vector && f.text.contains(text)),
        "{text}: {f:?}"
    );
    assert!(gate(&r, t, Mode::Refuse, None).is_err());
    assert!(ktpu_verify::schedule(&r[0], &Held::new(), t).is_err());
}

#[test]
fn missing_halt_immediates_loops_and_skip_boundaries_refuse() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let h = core(&t, "VHALT", &[]);
    let set = core(&t, "VSETI", &[("sreg", 0)]);
    let lp = core(&t, "VLOOP", &[("sreg", 0), ("body", 1)]);
    refused(&m, &t, &[core(&t, "VBAR", &[])], &[], "missing VHALT");
    refused(&m, &t, &[set], &[], "immediate");
    refused(&m, &t, &[lp, h], &[], "no loaded instruction");
    refused(&m, &t, &[lp, h, h], &[], "count not set");
    refused(
        &m,
        &t,
        &[
            set,
            2,
            core(&t, "VLOOP", &[("sreg", 0), ("body", 2)]),
            lp,
            h,
            h,
        ],
        &[],
        "nested",
    );
    refused(
        &m,
        &t,
        &[core(&t, "VSKIPZ", &[("sreg", 0), ("skip", 1)]), set, h, h],
        &[],
        "immediate",
    );
    for value in [0, i64::from(t.isa.vector.cfg.vlmax) + 1] {
        refused(
            &m,
            &t,
            &[set, value as u64, core(&t, "VSETVL", &[("sreg", 0)]), h],
            &[],
            "F_VL",
        );
    }
    // A zero-trip loop executes once; instruction-shaped immediate data is legal.
    let r = runs(&m, &t, &[set, 0, lp, core(&t, "VBAR", &[]), h], &[]);
    assert!(gate(&r, &t, Mode::Refuse, None).is_ok());
    assert!(gate(&runs(&m, &t, &[set, h, h], &[]), &t, Mode::Refuse, None).is_ok());
}

#[test]
fn scalar_control_joins_both_skip_arms_and_persists_across_runs() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let h = core(&t, "VHALT", &[]);
    let set = core(&t, "VSETI", &[("sreg", 1)]);
    let get = core(&t, "GETSTK", &[("sreg", 0)]);
    let skip = core(&t, "VSKIPZ", &[("sreg", 0), ("skip", 2)]);
    let vl = core(&t, "VSETVL", &[("sreg", 1)]);
    refused(&m, &t, &[set, 0, get, skip, set, 1, vl, h], &[], "F_VL");
    let post = core(&t, "VPOST", &[("dst_x", 2), ("dst_y", 1), ("n", 1)]);
    let skip1 = core(&t, "VSKIPZ", &[("sreg", 1), ("skip", 1)]);
    refused(
        &m,
        &t,
        &[set, 1, get, skip, set, 0, skip1, post, h],
        &[],
        "data-dependent skip",
    );
    let skip_header = core(&t, "VSKIPZ", &[("sreg", 0), ("skip", 1)]);
    for header in [
        core(&t, "VLOOP", &[("sreg", 1), ("body", 1)]),
        core(&t, "VSKIPZ", &[("sreg", 1), ("skip", 1)]),
    ] {
        refused(
            &m,
            &t,
            &[set, 2, get, skip_header, header, post, h],
            &[],
            "data-dependent skip",
        );
    }
    let set0 = core(&t, "VSETI", &[("sreg", 0)]);
    let lp = core(&t, "VLOOP", &[("sreg", 0), ("body", 3)]);
    for trips in [0, 1] {
        let r = runs(&m, &t, &[set0, trips, set, 1, lp, vl, set, 0, h], &[]);
        assert!(gate(&r, &t, Mode::Refuse, None).is_ok());
    }
    // VPOST-shaped data is consumed by VSETI, not by the skip's counter scan.
    let data = core(&t, "VPOST", &[("dst_x", 2), ("dst_y", 1), ("n", 1)]);
    assert!(gate(
        &runs(&m, &t, &[get, skip, set, data, h], &[]),
        &t,
        Mode::Refuse,
        None
    )
    .is_ok());
    let first = runs(&m, &t, &[set, 1, h], &[]).remove(0);
    let second = runs(&m, &t, &[vl, h], &[]).remove(0);
    let s = ktpu_verify::schedule(&first, &Held::new(), &t).unwrap();
    assert_eq!(s.held[&0][0].scalars, BTreeMap::from([(1, Some(1))]));
    assert!(ktpu_verify::schedule(&second, &s.held, &t).is_ok());
    assert!(gate(&[first, second], &t, Mode::Refuse, None).is_ok());
}

#[cfg(feature = "mutants")]
#[test]
fn reference_configuration_and_extended_register_decoder_ignore_mutants() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let expected = Isa::reference(&m).unwrap();
    ktpu_isa::mutant::with("isa.vector/consts", || {
        assert_ne!(Isa::new(&m).unwrap().vector.cfg, expected.vector.cfg);
        assert_eq!(Isa::reference(&m).unwrap().vector.cfg, expected.vector.cfg);
    });
    let word = ktpu_isa::unit::vector::VWord::Unpk {
        vd: m.vc.regs - 1,
        off: 0,
        n: 1,
        mode: ktpu_isa::unit::vector::WalkMode::F16,
        cb: 0,
        rel: false,
        q: 0,
    }
    .encode(&expected.vector)
    .unwrap();
    let decoded = expected.vector.decode_word_reference(word);
    ktpu_isa::mutant::with("isa.vector/rejoin_walk", || {
        assert_ne!(expected.vector.decode_word(word), decoded);
        assert_eq!(expected.vector.decode_word_reference(word), decoded);
    });
}

#[test]
fn source_sync_walk_and_descriptor_faults_refuse_before_allocation() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let h = core(&t, "VHALT", &[]);
    for (name, args, text) in [
        ("VMOV", vec![("vd", 0), ("sc", 2)], "F_SRC"),
        ("VCFG", vec![("sel", 15), ("payload", 0)], "VCFG"),
        ("VSYNC", vec![("waiter", 7)], "F_SYNC"),
        (
            "VWAIT",
            vec![("waiter", 5), ("n", 1), ("counter", 0)],
            "F_SYNC",
        ),
        (
            "VUNPK",
            vec![("reg", 0), ("n1", 0), ("mode", 1), ("off", 0)],
            "F_GT4",
        ),
        (
            "VUNPK",
            vec![("reg", 0), ("n1", 7), ("mode", 3), ("off", 0)],
            "F_MODE",
        ),
    ] {
        refused(&m, &t, &[core(&t, name, &args), h], &[], text);
    }
    let fill = core(&t, "VFILL", &[("ad", 0)]);
    refused(
        &m,
        &t,
        &[fill, h],
        &[(0, 1, dim(32, m.vc.walk_max + 1).unwrap())],
        "F_LEN",
    );
    refused(&m, &t, &[fill, h], &[(0, 1, dim(1, 2).unwrap())], "stride");
    refused(&m, &t, &[fill, h], &[(0, 0, 1)], "base");
    let bad = runs(&m, &t, &[h], &[]);
    let mut bad = bad[0].clone();
    bad.get_mut(&0).unwrap().payloads[0].0[1] |= 1;
    assert!(check(&[bad], &t).iter().any(|f| f.kind == Kind::Vector));
}

#[test]
fn capacities_follow_machine_and_wrapping_walks_remain_legal() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let words = [core(&t, "CHKVL", &[("vl1", 63)]), core(&t, "VHALT", &[])];
    assert!(gate(&runs(&m, &t, &words, &[]), &t, Mode::Refuse, None).is_ok());
    let small = m.with_fact("vc.vlmax", 32.into()).unwrap();
    let small = Target::from_machine(&small, Some(1)).unwrap();
    refused(&m, &small, &words, &[], "VL");
    let pack = [
        core(
            &t,
            "VPACK",
            &[("reg", 0), ("mode", 3), ("n1", 7), ("off", 0)],
        ),
        words[1],
    ];
    let no_mx = m.with_fact("vc.has_mx", 0.into()).unwrap();
    refused(
        &m,
        &Target::from_machine(&no_mx, Some(1)).unwrap(),
        &pack,
        &[],
        "F_MODE",
    );
    let walk = core(
        &t,
        "VUNPK",
        &[
            ("reg", 0),
            ("n1", 7),
            ("cb", 7),
            ("off", m.vc.l1_words as i64 - 1),
        ],
    );
    assert!(gate(
        &runs(&m, &t, &[walk, words[1]], &[]),
        &t,
        Mode::Refuse,
        None
    )
    .is_ok());
}

#[cfg(feature = "mutants")]
#[test]
fn rule_off_is_killed_and_ctl_corruption_refuses_on_all_machine_files() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../machines");
    let mut count = 0;
    for name in ktpu_machine::names(&root) {
        let m = Machine::named(&name).unwrap();
        if m.die.units.vc.is_empty() {
            continue;
        }
        let build = || {
            let isa = Isa::new(&m).unwrap();
            let h = isa.vector.core.halt.encode(&[]).unwrap().0[0];
            package(&m, &isa.vector.program(&[h], 0).unwrap())
        };
        let t = Target::from_machine(&m, Some(1)).unwrap();
        assert!(gate(&[Nodes::from([(0, build())])], &t, Mode::Refuse, None).is_ok());
        let bad = ktpu_isa::mutant::with("isa.vector/ctl_fields", build);
        let r = [Nodes::from([(0, bad)])];
        assert!(check(&r, &t).iter().any(|f| f.kind == Kind::Vector));
        let off =
            ktpu_verify::mutant::with("verify.vector/off", || gate(&r, &t, Mode::Refuse, None));
        assert!(off.is_ok(), "{name}: {off:?}");
        count += 1;
    }
    assert!(count > 0);
    eprintln!("ctl_fields statically refused and rule-off killed on {count} machine files");
}

#[cfg(feature = "mutants")]
#[test]
fn every_listed_ctl_fields_card_program_is_refused() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let source = std::fs::read_to_string(
        root.join("software/language/kohakutpu/language/examples/l2/fused/tilesilu.ktpu"),
    )
    .unwrap();
    let mut witnessed = 0;
    for entry in std::fs::read_dir(root.join("software/tests/spec/vectors/isa/vector")).unwrap() {
        let p = entry.unwrap().path();
        let v: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        if v["tier"] != 1
            || v["runner"] != "isa.card.example"
            || v["input"]["case"] != "tilesilu.l2"
            || !v["mutants"]
                .as_array()
                .is_some_and(|a| a.iter().any(|m| m == "isa.vector/ctl_fields"))
        {
            continue;
        }
        assert_eq!(v["input"]["case"], "tilesilu.l2");
        let m = Machine::named(v["machine"].as_str().unwrap()).unwrap();
        let build = || {
            let p = ktpu_l2::Program::parse(&source, "tilesilu", None).unwrap();
            let l1 =
                ktpu_l2::compile_l1(&p, &m, &ktpu_l2::PASSES, true, &ktpu_l2::Search::default())
                    .unwrap()
                    .l1;
            let binds = BTreeMap::from([
                ("x".into(), 0x100000),
                ("w".into(), 0x200000),
                ("y".into(), 0x300000),
            ]);
            let ps = ktpu_l1::build::programs(&l1, "tilesilu", &binds, &m, Some(1), None).unwrap();
            let b = ktpu_l1::build::Builder::new(&m).unwrap();
            ps.iter()
                .map(|p| {
                    (
                        p.mesh,
                        b.build(p, ktpu_l1::build::Opts::default())
                            .unwrap()
                            .build(false, false),
                    )
                })
                .collect::<Nodes>()
        };
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let clean = check(&[build()], &t);
        assert!(
            !clean.iter().any(|f| f.kind.refuses()),
            "{}: {clean:?}",
            p.display()
        );
        let bad = ktpu_isa::mutant::with("isa.vector/ctl_fields", build);
        let got = check(&[bad], &t);
        assert!(
            got.iter().any(|f| f.kind == Kind::Vector),
            "{}: {got:?}",
            p.display()
        );
        eprintln!("{} ctl_fields refused: {got:?}", p.display());
        witnessed += 1;
    }
    assert!(witnessed >= 4);
}
