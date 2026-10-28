use super::*;
use crate::lower::vector::gen::typed::{self as ir, Memory, Select};
use ktpu_machine::Machine;

fn core() -> Core {
    Core::of(&Target::of(&Machine::named("multimesh_v9t6").unwrap()))
}

fn paired(st: &Stmt, ids: &[u32], tmpl: &str, copies: u32, core: &Core) -> MOp {
    let syms: Vec<_> = ids.iter().copied().map(Symbol).collect();
    let old = parse(
        tmpl,
        &syms.iter().map(Symbol::name).collect::<Vec<_>>(),
        copies,
        core,
    );
    let mut got = derive(st, &syms, copies, core);
    assert!(got.tmpl.is_empty(), "typed derivation carries no template");
    got.tmpl = tmpl.into();
    assert_eq!(got, old, "{tmpl}, copies={copies}");
    got
}

#[test]
fn measured_mx7_issue_gap_changes_reservation_not_active_words() {
    let mut c = core();
    for mode in ["mx7a", "mx7b"] {
        let text = format!("vpack {{0}} -> l1[0] {mode}");
        assert_eq!(parse(&text, &["r".into()], 2, &c).occ, 17);
        c.timing.mx7_issue_gap += 1;
        assert_eq!(parse(&text, &["r".into()], 2, &c).occ, 18);
        c.timing.mx7_issue_gap -= 1;
        assert_eq!(c.timing.mx7_pack_words, 16);
    }
}

#[test]
fn memory_modes_counts_and_partial_writes() {
    let core = core();
    for (select, suffix) in [
        (Select::Whole, ""),
        (Select::Stride(0), "[0:]"),
        (Select::Stride(7), "[7:]"),
        (Select::Chunk(3), "[3]"),
    ] {
        for mode in ["f16", "f32", "gt4", "mx7a", "mx7b"] {
            for n in [None, Some(1), Some(4), Some(8)] {
                for (kind, op, arrow) in [
                    (Memory::Unpack, "vunpk", "<-"),
                    (Memory::Pack, "vpack", "->"),
                ] {
                    let options = n
                        .map(|n| vec![ir::kw("n", Term::int(n))])
                        .unwrap_or_default();
                    let st = ir::memory(
                        kind,
                        Symbol(11).operand(select),
                        37,
                        Some(mode),
                        true,
                        options,
                    )
                    .unwrap();
                    let tail = n.map(|n| format!(" n={n}")).unwrap_or_default();
                    let tmpl = format!("{op} {{0}}{suffix} {arrow} l1[37] {mode} rel{tail}");
                    for copies in [2, 3, 4] {
                        paired(&st, &[11], &tmpl, copies, &core);
                    }
                }
            }
        }
    }
}

#[test]
fn math_holds_selects_and_duplicate_sources() {
    let core = core();
    for op in ["vfma", "vfnma", "vsel"] {
        for ids in [[9, 1, 2, 3], [9, 1, 1, 3], [9, 1, 1, 1]] {
            for (select, suffix) in [
                (Select::Whole, ""),
                (Select::Stride(7), "[7:]"),
                (Select::Chunk(0), "[0]"),
            ] {
                for vl in [None, Some(0), Some(1), Some(17), Some(128)] {
                    let st = ir::math(
                        op,
                        Symbol(ids[0]).operand(Select::Whole),
                        ids[1..]
                            .iter()
                            .map(|&id| Symbol(id).operand(select))
                            .collect(),
                        vl.map(|n| vec![ir::kw("vl", Term::int(n))])
                            .unwrap_or_default(),
                    );
                    let tail = vl.map(|n| format!(" vl={n}")).unwrap_or_default();
                    let tmpl =
                        format!("{op} {{0}}, {{1}}{suffix}, {{2}}{suffix}, {{3}}{suffix}{tail}");
                    for copies in [2, 3, 4] {
                        let got = paired(&st, &ids, &tmpl, copies, &core);
                        let beats = core.beats(vl.map(|v| v as u64)) as i64;
                        assert_eq!(
                            got.occ,
                            beats * if copies == 2 && ids[2] == 2 { 2 } else { 1 }
                        );
                    }
                }
            }
        }
    }
    for slot in 0..3 {
        for crossing in [false, true] {
            let mut sources: Vec<_> = (1..=3)
                .map(|id| Symbol(id).operand(Select::Whole))
                .collect();
            let mut text = ["{1}".to_string(), "{2}".into(), "{3}".into()];
            if crossing {
                sources[slot] = ir::crossbar("xor", sources[slot].clone(), 1);
                text[slot] = format!("xor({}, 1)", text[slot]);
            } else {
                sources[slot] = Term::name("k1");
                text[slot] = "k1".into();
            }
            let st = ir::math("vfma", Symbol(9).operand(Select::Whole), sources, vec![]);
            let tmpl = format!("vfma {{0}}, {}", text.join(", "));
            for copies in [2, 3, 4] {
                paired(&st, &[9, 1, 2, 3], &tmpl, copies, &core);
            }
        }
    }
}

#[test]
fn cross_reads_predicates_and_ordered_dependencies() {
    let core = core();
    let reg = |id| Symbol(id).operand(Select::Whole);
    let mut a = vec![];
    let mut z = vec![];
    let fixtures = [
        (
            ir::math("vmov", reg(1), vec![Term::name("k1")], vec![]),
            vec![1],
            "vmov {0}, k1",
        ),
        (
            ir::math(
                "vmul",
                reg(2),
                vec![reg(1), ir::crossbar("xor", reg(1), 2)],
                vec![],
            ),
            vec![2, 1, 1],
            "vmul {0}, {1}, xor({2}, 2)",
        ),
        (
            ir::math("vcmplt", Term::name("p1"), vec![reg(1), reg(2)], vec![]),
            vec![99, 1, 2],
            "vcmplt p1, {1}, {2}",
        ),
        (
            ir::math(
                "vmov",
                reg(1),
                vec![Term::name("k0")],
                vec![ir::kw("if", Term::name("p1"))],
            ),
            vec![1],
            "vmov {0}, k0 if=p1",
        ),
        (
            ir::math(
                "vmov",
                reg(1),
                vec![reg(2)],
                vec![ir::kw("unless", Term::name("p1"))],
            ),
            vec![1, 2],
            "vmov {0}, {1} unless=p1",
        ),
        (
            ir::math(
                "vmul",
                reg(3),
                vec![Term::name("k1"), ir::bcast(reg(1), 3, 0)],
                vec![],
            ),
            vec![3, 1],
            "vmul {0}, k1, bcast({1}, lane=3, sh=0)",
        ),
    ];
    for (st, ids, tmpl) in fixtures {
        z.push(paired(&st, &ids, tmpl, 2, &core));
        a.push(parse(
            tmpl,
            &ids.iter().map(|&id| Symbol(id).name()).collect::<Vec<_>>(),
            2,
            &core,
        ));
    }
    assert_eq!(z[1].reads, ["%1"]);
    assert_eq!(z[1].xreads, ["%1"]);
    assert_eq!(z[2].writes, [PRED]);
    link(&mut a, &core.timing);
    link(&mut z, &core.timing);
    assert_eq!(z, a);
    assert_eq!(
        z[1].deps,
        [(0, core.timing.math_math + core.timing.xbar, true)]
    );
    assert!(z[3].deps.iter().any(|d| !d.2));
}
