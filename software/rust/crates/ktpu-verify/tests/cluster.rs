//! Device-free MG consistency and real-ISA corruption witnesses.

use std::collections::BTreeMap;
use std::path::Path;

use ktpu_isa::{ClusterCfg, ClusterIsa, W256};
use ktpu_l0::{Op, Package, Step, Unit, Word};
use ktpu_machine::Machine;
use ktpu_verify::{check, gate, Kind, Mode, Nodes, Target};
use serde_json::Value;

fn package(m: &Machine, words: &[W256]) -> Package {
    let c = &m.die.units.mg[0];
    Package {
        units: vec![Unit {
            type_code: u64::from_be_bytes(*b"\0\0\0\0\0\0MG"),
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

fn findings(m: &Machine, p: Package) -> Vec<ktpu_verify::Finding> {
    check(
        &[Nodes::from([(0, p)])],
        &Target::from_machine(m, Some(1)).unwrap(),
    )
}

#[test]
fn reserved_padding_is_refused_and_rule_off_is_killed() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let isa = ClusterIsa::of(ClusterCfg::of(&m).unwrap());
    let p = package(&m, &[isa.fill.encode(&[]).unwrap().with(0, 1, 1)]);
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let runs = [Nodes::from([(0, p)])];
    assert!(gate(&runs, &t, Mode::Refuse, None).is_err());
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with("verify.cluster/off", || {
        assert!(gate(&runs, &t, Mode::Refuse, None).is_ok());
    });
}

#[test]
fn fields_ranges_initialization_and_fused_drain_are_checked() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let isa = ClusterIsa::of(ClusterCfg::of(&m).unwrap());
    for (op, args, text) in [
        (
            "FILL",
            vec![("n", m.mg.max_fill as i64 + 1)],
            "count/offset",
        ),
        ("FILL", vec![("fuse", 1)], "unused fuse"),
        ("FILL", vec![("recv", 1), ("addr", 32)], "receive FILL"),
        ("FILL", vec![("npeer", 1), ("peers", 0)], "peer"),
        ("GEMM", vec![("gm", 1), ("gn", 1)], "uninitialized"),
        ("GEMM", vec![("gm", 0), ("gn", 1)], "shape"),
        ("DRAIN", vec![("n", 1), ("fuse", 1)], "matching emitting"),
        ("DRAIN", vec![("dflags", 4)], "unknown drain flags"),
        ("DRAIN", vec![("dfin", 1)], "memory drain"),
        ("POST", vec![("dst_x", 0), ("dst_y", 0)], "destination"),
    ] {
        let w = isa.set.by_name(op).unwrap().encode(&args).unwrap();
        let got = findings(&m, package(&m, &[w]));
        assert!(
            got.iter()
                .any(|f| f.kind == Kind::Cluster && f.text.contains(text)),
            "{op} {args:?}: {got:?}"
        );
    }
    let mut w = isa.fill.encode(&[]).unwrap();
    w = w.with(0, 1, 1);
    assert!(findings(&m, package(&m, &[w]))
        .iter()
        .any(|f| f.text.contains("padding")));
}

#[cfg(feature = "mutants")]
fn tiles(m: &Machine) -> Package {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let source = std::fs::read_to_string(
        root.join("software/language/kohakutpu/language/examples/l1/matmul/tiles.ktpu"),
    )
    .unwrap();
    let module = ktpu_l1::text::read(&source, "tiles.ktpu").unwrap();
    let binds = BTreeMap::from([
        ("cnt".into(), 0x10000),
        ("x".into(), 0x100000),
        ("w".into(), 0x200000),
        ("y".into(), 0x300000),
    ]);
    let programs = ktpu_l1::build::programs(&module, "tiles", &binds, m, Some(1), None).unwrap();
    ktpu_l1::build::Builder::new(m)
        .unwrap()
        .build(&programs[0], ktpu_l1::build::Opts::default())
        .unwrap()
        .build(false, false)
}

#[cfg(feature = "mutants")]
#[test]
fn all_card_tiles_vectors_refuse_both_isa_mutants_offline() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../../software/tests/spec/vectors/isa/cluster");
    let mut witnessed = 0;
    for entry in std::fs::read_dir(root).unwrap() {
        let p = entry.unwrap().path();
        if !p
            .file_name()
            .unwrap()
            .to_string_lossy()
            .ends_with(".card_tiles.json")
        {
            continue;
        }
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        if !v["mutants"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s == "isa.cluster/fields")
        {
            continue;
        }
        assert_eq!(v["input"]["case"], "tiles");
        let m = Machine::named(v["machine"].as_str().unwrap()).unwrap();
        let clean = findings(&m, tiles(&m));
        assert!(
            !clean.iter().any(|f| f.kind.refuses()),
            "{}: {clean:?}",
            p.display()
        );
        for mutant in ["isa.cluster/fields", "isa.cluster/opcodes"] {
            let bad = ktpu_isa::mutant::with(mutant, || tiles(&m));
            let got = findings(&m, bad.clone());
            assert!(
                got.iter()
                    .any(|f| f.kind == Kind::Cluster && f.kind.refuses()),
                "{} {mutant}: {got:?}",
                p.display()
            );
            let off = ktpu_verify::mutant::with("verify.cluster/off", || findings(&m, bad));
            assert!(
                !off.iter().any(|f| f.kind == Kind::Cluster),
                "rule-off survived"
            );
            eprintln!(
                "{} {mutant}: statically refused; verify.cluster/off killed",
                p.display()
            );
        }
        witnessed += 1;
    }
    assert!(
        witnessed >= 14,
        "missing the brief's 14 witnesses: {witnessed}"
    );
    eprintln!("{witnessed} card_tiles vectors, both ISA mutants refused offline");
}

#[cfg(feature = "mutants")]
#[test]
fn isa_corruption_is_refused_on_every_machine_with_clusters() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../machines");
    let mut targets = 0;
    for name in ktpu_machine::names(&root) {
        let m = Machine::named(&name).unwrap();
        if m.die.units.mg.is_empty() {
            continue;
        }
        let program = || {
            let isa = ClusterIsa::new(&m).unwrap();
            package(
                &m,
                &[
                    isa.fill.encode(&[("n", 1)]).unwrap(),
                    isa.fill.encode(&[("n", 1), ("sel", 1)]).unwrap(),
                    isa.gemm.encode(&[("gm", 1), ("gn", 1)]).unwrap(),
                    isa.drain.encode(&[("n", 1)]).unwrap(),
                ],
            )
        };
        let clean = findings(&m, program());
        assert!(!clean.iter().any(|f| f.kind.refuses()), "{name}: {clean:?}");
        for mutant in ["isa.cluster/fields", "isa.cluster/opcodes"] {
            let bad = ktpu_isa::mutant::with(mutant, program);
            let got = findings(&m, bad);
            assert!(
                got.iter().any(|f| f.kind == Kind::Cluster),
                "{name} {mutant}: {got:?}"
            );
        }
        targets += 1;
    }
    assert!(targets > 0);
    eprintln!("both cluster ISA mutants refused on {targets} machine files");
}

#[test]
fn emissions_are_collected_once_in_order() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let isa = ClusterIsa::of(ClusterCfg::of(&m).unwrap());
    let a = isa.fill.encode(&[("n", 1)]).unwrap();
    let b = isa.fill.encode(&[("n", 1), ("sel", 1)]).unwrap();
    let g = isa
        .gemm
        .encode(&[("gm", 1), ("gn", 1), ("emit", 1), ("addr", 4096)])
        .unwrap();
    let d = isa
        .drain
        .encode(&[("n", 1), ("fuse", 1), ("addr", 4096)])
        .unwrap();
    for (words, refused) in [
        (vec![a, b, g], true),
        (vec![a, b, g, d], false),
        (vec![a, b, g, g, d], true),
        (vec![a, b, g, d, d], true),
    ] {
        let got = findings(&m, package(&m, &words));
        assert_eq!(got.iter().any(|f| f.kind.refuses()), refused, "{got:?}");
    }
}

#[test]
fn padding_and_credit_marker_goldens_remain_accepted() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e");
    for case in [
        "l2-low-L2.LOW.014.spend_192",
        "l2-low-L2.LOW.014.stream_card",
    ] {
        let v: Value = serde_json::from_str(
            &std::fs::read_to_string(root.join(format!("{case}.json"))).unwrap(),
        )
        .unwrap();
        let m = Machine::named(v["machine"].as_str().unwrap())
            .unwrap()
            .with_nodes(v["nodes"].as_u64().unwrap() as u32)
            .unwrap();
        let module = ktpu_l1::text::read(v["l1"].as_str().unwrap(), case).unwrap();
        let binds: BTreeMap<_, _> = v["binds"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(n, a)| (n.clone(), a.as_i64().unwrap() as i128))
            .collect();
        let programs = ktpu_l1::build::programs(
            &module,
            v["name"].as_str().unwrap(),
            &binds,
            &m,
            Some(m.nodes()),
            None,
        )
        .unwrap();
        let builder = ktpu_l1::build::Builder::new(&m).unwrap();
        let nodes: Nodes = programs
            .iter()
            .map(|p| {
                (
                    p.mesh,
                    builder
                        .build(p, ktpu_l1::build::Opts::default())
                        .unwrap()
                        .build(false, false),
                )
            })
            .collect();
        let got = check(&[nodes], &Target::from_machine(&m, None).unwrap());
        assert!(!got.iter().any(|f| f.kind.refuses()), "{case}: {got:?}");
    }
}
