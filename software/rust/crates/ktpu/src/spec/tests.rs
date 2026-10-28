use super::*;

#[test]
fn dualq_keeps_unpack_and_pack_queues_independent() {
    let m = Machine::named("multimesh_v9t6")
        .unwrap()
        .with_fact("vc.dualq", json!(1))
        .unwrap();
    let text = "image k()\n    vunpk v0 <- l1[0] f16 n=1 q=0\n    vunpk v1 <- l1[1] f16 n=1 q=1\n    vpack v0 -> l1[2] f16 n=1 q=0\n    vpack v1 -> l1[3] f16 n=1 q=1\n    halt\n\nfn k.l1()\n    send vc[0]\n        run k()\n    barrier\n";
    let got = verify(&json!({"text": text, "fn": "k", "l1": true}), &m).unwrap();
    assert_eq!(
        got,
        json!({"findings": [], "l1": {"vc0": {"read": [0,1], "written": [2,3]}}})
    );
}

#[test]
fn dualq_queue_one_bypasses_a_waiting_queue_zero() {
    let m = Machine::named("multimesh_v9t6")
        .unwrap()
        .with_fact("vc.dualq", json!(1))
        .unwrap();
    let target = ktpu_verify::Target::from_machine(&m, None).unwrap();
    for (waiter, walk, write) in [
        ("unpack", "vunpk v1 <- l1[7] f16 n=1 q=1", false),
        ("pack", "vpack v1 -> l1[7] f16 n=1 q=1", true),
    ] {
        let text = format!("image k()\n    vwait {waiter} rx n=1 q=0\n    {walk}\n    halt\n\nfn k.l1()\n    send vc[0]\n        run k()\n    barrier\n");
        let module = ktpu_l1::text::read(&text, "<dualq>").unwrap();
        let progs =
            ktpu_l1::build::programs(&module, "k", &BTreeMap::new(), &m, None, None).unwrap();
        let builder = ktpu_l1::build::Builder::new(&m).unwrap();
        let built = builder
            .build(&progs[0], ktpu_l1::build::Opts::default())
            .unwrap();
        let mut nodes = ktpu_verify::Nodes::new();
        nodes.insert(0, built.build(false, false));
        let touched = || ktpu_verify::check::l1_touched(&nodes, &target);
        let got = touched();
        assert!(
            got.iter().any(|(_, r, w)| if write {
                w.contains(&7)
            } else {
                r.contains(&7)
            }),
            "{got:?}"
        );
        #[cfg(feature = "mutants")]
        {
            let broken = ktpu_verify::mutant::with("rul.003/queues-folded", touched);
            assert!(
                !broken.iter().any(|(_, r, w)| if write {
                    w.contains(&7)
                } else {
                    r.contains(&7)
                }),
                "{broken:?}"
            );
        }
    }
}

#[test]
fn integers_preserve_unsigned_values_and_reject_nonintegers() {
    for n in [i64::MAX as u64, i64::MAX as u64 + 1, u64::MAX] {
        assert_eq!(integer(&json!(n)), Ok(n as i128));
    }
    assert_eq!(integer(&json!(i64::MIN)), Ok(i64::MIN as i128));
    for value in [json!(null), json!(1.5), json!("42")] {
        assert!(integer(&value).is_err());
    }
}
