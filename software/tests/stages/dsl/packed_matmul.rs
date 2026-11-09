//! Packed epilogues retain complete destination chunks under split ownership.
use super::*;
use ktpu_kernels::gen::matmul::{self, split as matrix, QDemand, Side};
use ktpu_kernels::gen::vector::{Kind, Operand};
use ktpu_kernels::gen::Hw;
use ktpu_kernels::split::cost::ChoiceCache;
use ktpu_kernels::split::SplitAxis;
use ktpu_kernels::trace::buffer::{sl, spec, ALL};
use ktpu_kernels::trace::control::At;
use ktpu_kernels::trace::Arg;

fn two_matrix_units() -> Machine {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(5)
        .expect("repository");
    Machine::load(root.join("machines/multimesh_v9t6.json")).expect("machine")
}

#[test]
fn split_packed_epilogues_compile_and_write_exact_bands() {
    let m = two_matrix_units();
    let hw = Hw::of(&m);
    let knobs = matmul::defaults();
    let mm = matmul::plan(
        1,
        197,
        768,
        768,
        true,
        hw.band,
        ktpu_machine::Pairing::Vector,
        &m,
        &knobs,
    )
    .expect("product");
    let e = ktpu_kernels::expr::from_table(&json!({
        "nodes": [{"load": 0}, {"load": 1}, {"op": "add", "args": [0, 1]}],
        "root": 2
    }))
    .expect("biased product");
    let extras = [Operand::new(Kind::Col)];
    let out = Operand::full(mm.n, mm.mp);
    let made = BTreeMap::new();
    let demands = [QDemand {
        side: Side::A,
        groups: mm.mp / hw.band,
        nk: hw.kstep,
        rp: mm.mp,
        cp: mm.n,
    }];
    let shape = matrix::Shape {
        mm: &mm,
        class: None,
        e: &e,
        extras: &extras,
        out: &out,
        knobs: &knobs,
        made: &made,
        demands: &demands,
    };
    let mut plans = Vec::new();
    for nodes in [vec![0, 1], vec![0, 1, 2, 3]] {
        let mut req = request(&m);
        req.nodes = NodeSet::new(nodes, &m).expect("grant");
        let options = matrix::split_options(&shape, &req, &m).expect("local tile alternatives");
        let joint = options
            .iter()
            .find(|option| {
                option.id.starts_with("n-tile-")
                    && matches!(option.axis, SplitAxis::Tiled { tm, .. } if tm == mm.mp)
            })
            .expect("common row tile");
        for id in ["n", joint.id.as_str(), "n-owner-waves-2"] {
            req.force = Some(id.into());
            let plan = matrix::kernel(&shape, &req, &m).expect("packed column owners");
            validate_ownership(&plan.split.output).expect("whole destination owners");
            plans.push(plan);
        }
    }

    // Only K=0 contributes. Row signs and column magnitudes distinguish
    // owners; the paired empty block still participates in exponent alignment.
    let owner_columns = mm.n / 4;
    let magnitude = |col: i64| ((col / owner_columns + col) % 4) as usize;
    let input = |side, group, block, lane, k| {
        if block != 0 || k != 0 {
            return 0;
        }
        let row = group * hw.band + lane;
        if side {
            [0x3800, 0x3c00, 0x4000, 0x4400][magnitude(row)]
        } else if row < mm.m {
            if row % 2 == 0 {
                0x3c00
            } else {
                0xbc00
            }
        } else {
            0
        }
    };
    let reference = matmul::mm_kernel(&mm, &e, &extras, &out, &knobs, &m)
        .expect("single-node MXFP7 product and FP16 epilogue");
    let (reference, _) = simulate_matmul_executable(&reference, None, &input, &|_| 0x3400, &m);
    let packed = SimBuf {
        at: 0,
        dtype: "mx7a".into(),
        shape: vec![mm.mp, mm.n],
        layout: Layout::Mx7 {
            b: false,
            g: demands[0].groups,
            nk: demands[0].nk,
        },
    };
    let mut expected = vec![0; packed.bytes() as usize];
    for group in 0..mm.mp / hw.band {
        for block in 0..mm.n / hw.kblock {
            let mut values = [[0u16; 32]; 4];
            for (lane, row_values) in values.iter_mut().enumerate() {
                let row = group * hw.band + lane as i64;
                for (k, value) in row_values.iter_mut().enumerate() {
                    let col = block * hw.kblock + k as i64;
                    *value = if row >= mm.m {
                        0
                    } else {
                        let at = ((row * mm.n + col) * 2) as usize;
                        u16::from_le_bytes([reference[at], reference[at + 1]])
                    };
                }
            }
            let at = packed.entry(group, block).expect("entry address") as usize;
            for (word, bytes) in mx7::entry(&values).write(false).iter().enumerate() {
                let start = at + word * hw.word_bytes as usize;
                expected[start..start + bytes.len()].copy_from_slice(bytes);
            }
        }
    }
    for plan in plans {
        let (actual, writes) =
            simulate_named_executable(&plan.kernel, None, "q0", &input, &|_| 0x3400, &[], &m);
        assert_eq!(
            actual, expected,
            "{} at P={}: biased products and zeroed padding",
            plan.split.id, plan.split.p,
        );
        assert!(
            writes.iter().all(|&count| count == 1),
            "{} at P={}: one packed writer",
            plan.split.id,
            plan.split.p,
        );
    }
}

#[test]
fn lowering_refusals_remain_in_the_priced_comparison() {
    let m = two_matrix_units();
    let hw = Hw::of(&m);
    let mut t = Trace::new("partial_packed_chunk", Facts::of(&m), &m.die.target);
    let source = t
        .port(
            "x",
            ktpu_graph::Role::In,
            f16(&[2 * hw.band, hw.kblock]).expect("source"),
        )
        .expect("source port");
    let destination = t
        .port(
            "q",
            ktpu_graph::Role::Out,
            spec(
                &[2 * hw.band, hw.kstep * hw.kblock],
                ktpu_graph::Dtype::Mx7a,
                ktpu_kernels::kernel::layout(&[("gm", 2), ("nk", hw.kstep)]),
                ktpu_graph::BufSpace::Dram,
            )
            .expect("paired packing"),
        )
        .expect("destination port");
    t.par(1, ktpu_graph::UnitKind::Vc, At::Var, |t, _| {
        t.range(2, |t, i| {
            let row = i.mul(hw.band);
            let x = t.load(&source.at(&[sl(&row, row.add(hw.band)), ALL])?, true)?;
            let x = t.unpack(x, None)?;
            let q = t.pack(x, ktpu_graph::Dtype::Mx7a, true)?;
            t.store(
                &destination.at(&[sl(&row, row.add(hw.band)), sl(0, hw.kblock)])?,
                Arg::Val(q),
            )
        })
    })
    .expect("candidate graph");
    let graph = t.graph().expect("graph");
    let good = call_split(
        "convert.copy_kernel",
        &json!({"nbytes": 128}),
        &request(&m),
        &m,
    )
    .expect("legal candidate")
    .split;
    let mut bad = good.clone();
    bad.id = "partial-chunk".into();
    let candidates = vec![(bad, Some(0)), (good.clone(), None)];
    let price = |option: ktpu_kernels::split::SplitOption| {
        if option.id == "partial-chunk" {
            ktpu_graph::node_cycles(&graph, &m)?;
        }
        Ok(option)
    };
    let cache = ChoiceCache::default();
    let chosen = cache
        .choose_bounded(
            "lowering".into(),
            None,
            || Ok(candidates.clone()),
            price,
            |_| Ok(()),
        )
        .expect("a refused price cannot abort or prune the legal candidate");
    assert_eq!(chosen.id, good.id);
    assert_eq!(chosen.cost.makespan, good.cost.makespan);
    assert!(chosen.legality.clauses.iter().any(|reason| {
        reason.contains("refused partial-chunk:") && reason.contains("L2.COST.001")
    }));
    let error = cache
        .choose_bounded(
            "lowering".into(),
            Some("partial-chunk"),
            || Ok(candidates.clone()),
            price,
            |_| Ok(()),
        )
        .expect_err("forcing a refused candidate cannot select another one");
    assert_eq!(error.clause, "DSL.KERN.020");
    assert!(error.message.contains("partial-chunk"));
    assert!(error.message.contains("whole chunks"), "{error}");
}
