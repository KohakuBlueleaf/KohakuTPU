//! Rows offsets and statistic-first decomposition, without a card or model.
use super::*;
use crate::exec::Exec;
use crate::program::Step;
use crate::uop::Graph;
use ktpu_machine::Machine;

fn addr(offset: i64) -> Addr {
    Addr {
        buf: "input".into(),
        form: Value::Null,
        offset,
    }
}

fn byte(o: &Operand, at: i64, r: i64, c: i64) -> i64 {
    let index = match o.kind.as_str() {
        "scalar" => o.offset,
        "row" | "col" => o.offset + r,
        _ => o.offset + r * o.pitch + c,
    };
    at + index * if o.dtype == "f32" { 4 } else { 2 }
}

fn offsets_preserved() -> bool {
    let m = Machine::named("multimesh_v9t8").unwrap();
    let runner = Runner::new(&m, Graph::default(), Exec::default());
    let cases = [
        // Expected byte shift, retained element offset, remaining rows.
        ("full", "f16", 96, 320, 10, 576, 32, 7),
        ("tile", "f16", 96, 320, 10, 576, 32, 7),
        ("full", "f16", 48, 7, 4, 0, 7, 4),
        ("full", "f32", 64, 8, 4, 0, 8, 4),
        ("col", "f16", 0, 48, 0, 96, 0, 0),
        ("row", "f16", 0, 5, 9, 0, 5, 9),
        ("row", "f16", 0, 32, 40, 64, 0, 8),
        ("scalar", "f16", 0, 2, 0, 0, 2, 0),
        ("scalar", "f16", 0, 18, 0, 32, 2, 0),
    ];
    for (kind, dtype, pitch, offset, rows, shift, retained, remaining) in cases {
        let o = Operand {
            kind: kind.into(),
            dtype: dtype.into(),
            pitch,
            offset,
            rows,
        };
        let mut binds = BTreeMap::from([("in0".into(), addr(4096))]);
        let got = runner
            .based(std::slice::from_ref(&o), &mut binds, "in")
            .remove(0);
        for (r, c) in [(0, 0), (1, 5), (2, 31)] {
            if byte(&got, binds["in0"].offset, r, c) != byte(&o, 4096, r, c) {
                return false;
            }
        }
        if binds["in0"].offset != 4096 + shift || got.offset != retained || got.rows != remaining {
            return false;
        }
    }
    true
}

fn column_extent_preserved() -> bool {
    let m = Machine::named("multimesh_v9t8").unwrap();
    let mut runner = Runner::new(&m, Graph::default(), Exec::default());
    // Three live rows, after two leading rows, end at the allocation's last byte.
    let original = Operand {
        pitch: 80,
        offset: 176,
        rows: 5,
        ..Operand::new("full")
    };
    runner
        .launch_rows(
            &expr::load(0),
            std::slice::from_ref(&original),
            BTreeMap::from([("in0".into(), addr(4096)), ("out".into(), addr(8192))]),
            3,
            64,
            &original,
            Value::Null,
        )
        .unwrap();
    let Step::Launch(launch) = runner.x.steps.last().unwrap() else {
        return false;
    };
    let expected = Operand {
        offset: 16,
        rows: 3,
        ..original.clone()
    };
    let call = launch.call.as_ref().unwrap();
    if call.args["operands"][0] != expected.json() || call.args["out"] != expected.json() {
        return false;
    }
    for (port, base) in [("in0", 4096), ("out", 8192)] {
        let at = launch.binds[port].offset;
        if at != base + 320 || at + 3 * 80 * 2 != base + 5 * 80 * 2 {
            return false;
        }
        for r in 0..3 {
            for c in 0..64 {
                if byte(&expected, at, r, c) != byte(&original, base, r, c) {
                    return false;
                }
            }
        }
        if byte(&expected, at, 2, 63) + 2 != base + 5 * 80 * 2 {
            return false;
        }
    }
    true
}

fn statistic_runs() -> bool {
    let m = Machine::named("multimesh_v9t8").unwrap();
    let mut runner = Runner::new(&m, Graph::default(), Exec::default());
    let w = 4 * runner.max_block();
    let x = expr::load(0);
    let mean = expr::op(
        "mul",
        vec![
            expr::reduce("sum", x.clone()),
            expr::constant(1.0 / w as f64),
        ],
    );
    let d = expr::op("sub", vec![x, mean]);
    let var = expr::op(
        "mul",
        vec![
            expr::reduce("sum", expr::op("mul", vec![d.clone(), d.clone()])),
            expr::constant(1.0 / w as f64),
        ],
    );
    let e = expr::op(
        "mul",
        vec![
            d,
            expr::op(
                "rsqrt",
                vec![expr::op("add", vec![var, expr::constant(1e-5)])],
            ),
        ],
    );
    let o = Operand {
        pitch: w,
        rows: 8,
        ..Operand::new("full")
    };
    runner
        .rows_run(
            &e,
            std::slice::from_ref(&o),
            &BTreeMap::from([("in0".into(), addr(0))]),
            8,
            w,
            &o,
            &addr(2 * 8 * w),
            &Value::Null,
        )
        .unwrap();
    crate::program::compile(&mut runner.x.steps, &m).unwrap();
    let launches: Vec<_> = runner
        .x
        .steps
        .iter()
        .filter_map(|s| match s {
            Step::Launch(l) => Some(l),
            _ => None,
        })
        .collect();
    launches.len() >= 3
        && launches
            .last()
            .is_some_and(|l| l.binds.get("out") == Some(&addr(2 * 8 * w)))
}

#[test]
fn an_offset_moves_into_the_address_reading_the_same_bytes() {
    assert!(offsets_preserved());
    assert!(column_extent_preserved());
}
#[test]
fn two_reductions_across_blocks_compile() {
    assert!(statistic_runs());
}
#[cfg(feature = "mutants")]
#[test]
fn rows_deletion_witnesses_kill_mutants() {
    assert!(!mutant::with("tg.rows/based_zero", offsets_preserved));
    assert!(!mutant::with(
        "tg.rows/based_column",
        column_extent_preserved
    ));
    assert!(!mutant::with("tg.rows/stat_first_skip", statistic_runs));
    assert!(!mutant::with("tg.rows/stat_continue_skip", statistic_runs));
}
