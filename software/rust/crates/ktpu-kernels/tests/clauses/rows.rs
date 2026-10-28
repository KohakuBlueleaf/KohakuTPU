//! Rows and flat kernels: API.ROW, API.FORM.010, API.READ.

use super::{card, lines, text};
use ktpu_graph as g;
use ktpu_kernels::expr::{self, RedOp};
use ktpu_kernels::gen::vector::{self as v, Operand};
use ktpu_kernels::gen::Hw;
use ktpu_kernels::trace::Idx;
use ktpu_kernels::Kind;
use std::collections::HashMap;

fn mul2() -> expr::E {
    expr::op("mul", vec![expr::load(0).into(), 2.0.into()]).unwrap()
}

/// API.ROW.001's facts: the ports' types and the column blocks.
pub fn row_001_facts() -> (Vec<(String, String)>, i64) {
    let full = Operand::full(64, 8);
    let (k, nblk) = v::rows_kernel(&mul2(), &[full], 8, 64, &full, None, &card()).unwrap();
    let params = ktpu_graph::lower(&k.graph).unwrap().params;
    (params, nblk)
}

#[test]
fn api_row_001_rows_kernel_of_whole_blocks() {
    let want = vec![
        ("in0".to_string(), "f16[8, 64] @dram".to_string()),
        ("out".to_string(), "f16[8, 64] @dram".to_string()),
    ];
    assert_eq!(row_001_facts(), (want, 1));
}

/// API.ROW.002's facts: `[wc, nblk, rows, nb, rp]` of three plans.
pub fn row_002_facts() -> Vec<[i64; 5]> {
    let hw = Hw::of(&card());
    let full = [Operand::new(Kind::Full)];
    [
        v::plan(&full, Kind::Full, 64, 64, None, None, 4, &hw),
        v::plan(&full, Kind::Full, 64, 512, None, None, 4, &hw),
        v::plan(&full, Kind::Row, 40, 64, None, None, 16, &hw),
    ]
    .into_iter()
    .map(|p| {
        let p = p.unwrap();
        [p.wc, p.nblk, p.rows, p.nb, p.rp]
    })
    .collect()
}

#[test]
fn api_row_002_bands_and_blocks() {
    assert_eq!(
        row_002_facts(),
        vec![[64, 1, 16, 4, 64], [256, 2, 4, 16, 64], [64, 1, 16, 3, 48]]
    );
}

#[test]
fn api_row_003_wide_row_partials() {
    let e = expr::reduce(RedOp::Sum, expr::load(0));
    let (k, nblk) = v::rows_kernel(
        &e,
        &[Operand::full(1024, 24)],
        24,
        1024,
        &Operand::new(Kind::Row),
        None,
        &card(),
    )
    .unwrap();
    // Four bands of 16 statistic rows fit a block of 128 columns.
    assert_eq!(nblk, 8);
    let out = k.graph.ports.iter().find(|p| p.name == "out").unwrap();
    assert_eq!(out.spec.shape, vec![8, 32]);
}

/// A one-hot over a per-row statistic and a broadcast column row
/// (LeNet's labels): every op takes the rows of the widest operand,
/// the statistic's rows included, never only the broadcast row's.
#[test]
fn api_row_math_takes_a_statistics_rows() {
    let row = expr::op("cmpne", vec![expr::load(0).into(), expr::load(1).into()]).unwrap();
    let e = expr::op("cmpne", vec![row.into(), 1.0.into()]).unwrap();
    let ops = [Operand::new(Kind::Row), Operand::new(Kind::Col)];
    let (k, _) = v::rows_kernel(&e, &ops, 32, 32, &Operand::full(32, 32), None, &card()).unwrap();
    let t = text(&k);
    let rows_of = |op: &str| -> Vec<i64> {
        t.lines()
            .filter(|l| l.contains(&format!("= {op} ")))
            .filter_map(|l| l.rsplit_once(": f32[").map(|(_, s)| s))
            .filter_map(|s| s.split([',', ']']).next().unwrap().trim().parse().ok())
            .collect()
    };
    let stat = rows_of("unpack");
    let cmp: Vec<i64> = ["cmpeq", "sub"].iter().flat_map(|op| rows_of(op)).collect();
    assert!(
        !cmp.is_empty() && !stat.is_empty(),
        "no typed math / unpack in\n{t}"
    );
    let widest = stat.iter().copied().max().expect("an unpack");
    assert!(
        cmp.iter().all(|&r| r == widest),
        "cmpne rows {cmp:?}, operands {stat:?}\n{t}"
    );
}

/// A stage's `(rows, first row, real rows)`.
pub type Stage = (i64, i64, Option<i64>);

/// API.ROW.004's facts: the stages and the mover lines.
pub fn row_004_facts() -> (Vec<Stage>, Vec<String>) {
    let hw = Hw::of(&card());
    let full = Operand::full(32, 6);
    let pl = v::plan(&[full], Kind::Full, 6, 32, None, None, 4, &hw).unwrap();
    let st = v::stages(&pl, 6, 4)
        .into_iter()
        .map(|(s, r0, n)| (s.rows, r0, n))
        .collect();
    let (k, _) = v::rows_kernel(&mul2(), &[full], 6, 32, &full, None, &card()).unwrap();
    let moves = text(&k)
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| matches!(l.split(' ').next(), Some("copy" | "fill")))
        .collect();
    (st, moves)
}

#[test]
fn api_row_004_last_partial_band() {
    let (st, moves) = row_004_facts();
    assert_eq!(st, vec![(4, 0, None), (4, 4, Some(2))]);
    assert_eq!(
        moves,
        vec![
            "fill h1 value=0",
            "copy h1[0 : +2] <- in0[4 : +2, 0 : +32]",
            "copy out[4 : +2, 0 : +32] <- h2[0 : +2]",
        ]
    );
}

/// API.ROW.006's facts: the flat kernel's stores.
pub fn row_006_facts() -> Vec<String> {
    let e = expr::op("add", vec![expr::load(0).into(), expr::load(1).into()]).unwrap();
    let k = v::flat_kernel(
        &e,
        &[g::Dtype::F16; 2],
        21 * 128,
        g::Dtype::F16,
        &[],
        &card(),
    )
    .unwrap();
    lines(&k, "store")
}

/// `ix` at the variables' values (an atom's floor, mod and product too).
fn eval(ix: &g::Index, env: &HashMap<String, i64>) -> i64 {
    match ix {
        g::Index::Int(n) => *n,
        g::Index::Sym(s) => {
            s.konst
                + s.terms
                    .iter()
                    .map(|(a, c)| {
                        c * match a {
                            g::Atom::Var(v) => env[v],
                            g::Atom::Floor(a, b) => eval(a, env).div_euclid(eval(b, env)),
                            g::Atom::Mod(a, b) => eval(a, env).rem_euclid(eval(b, env)),
                            g::Atom::Mul(a, b) => eval(a, env) * eval(b, env),
                        }
                    })
                    .sum::<i64>()
        }
    }
}

/// `(buffer, first row, rows, written)` of every view a statement reads or
/// writes, at every value of the `par` / `for` variables around it.
fn views(body: &[g::Stmt], env: &HashMap<String, i64>, out: &mut Vec<(String, i64, i64, bool)>) {
    let row = |v: &g::View, written: bool, out: &mut Vec<_>| {
        if let Some(g::Axis::Run(first, n)) = v.axes.first() {
            out.push((v.buffer.clone(), eval(first, env), *n, written));
        }
    };
    for s in body {
        match &s.node {
            g::Node::Par {
                var, lo, hi, body, ..
            }
            | g::Node::For {
                var, lo, hi, body, ..
            } => {
                for x in eval(lo, env)..eval(hi, env) {
                    let mut inner = env.clone();
                    inner.insert(var.clone(), x);
                    views(body, &inner, out);
                }
            }
            g::Node::When { body, .. } | g::Node::Sticky { body } => views(body, env, out),
            g::Node::Value { args, .. } => {
                for a in args {
                    if let g::Operand::View(v) = a {
                        row(v, false, out);
                    }
                }
            }
            g::Node::Store { dst, .. } => row(dst, true, out),
            g::Node::Mover { dst, src, .. } => {
                row(dst, true, out);
                if let Some(v) = src {
                    row(v, false, out);
                }
            }
            _ => {}
        }
    }
}

/// For R in 1, 3, 5, 6, 17, 30, 63 rows of `x + 1` over [R, 128]: every row
/// the kernel reads or writes of `in0` / `out` past R, and the rows of R
/// `out` misses or gets twice (`application/tests/kernels/test_vector.py`).
pub fn row_004_past_r_facts() -> Vec<String> {
    let e = expr::op("add", vec![expr::load(0).into(), 1.0.into()]).unwrap();
    let mut bad = vec![];
    for r in [1, 3, 5, 6, 17, 30, 63] {
        let (k, _) = v::rows_kernel(
            &e,
            &[Operand::full(128, r)],
            r,
            128,
            &Operand::full(128, 0),
            None,
            &card(),
        )
        .unwrap();
        let mut seen = vec![];
        views(&k.graph.body, &HashMap::new(), &mut seen);
        let mut written = vec![0; r as usize];
        for (buf, first, n, w) in seen {
            if buf != "in0" && buf != "out" {
                continue;
            }
            if first + n > r {
                bad.push(format!("R {r}: {buf}[{first} : +{n}] past R"));
            }
            if buf == "out" && w {
                for x in first..(first + n).min(r) {
                    written[x as usize] += 1;
                }
            }
        }
        if let Some(x) = written.iter().position(|c| *c != 1) {
            bad.push(format!("R {r}: out row {x} written {} times", written[x]));
        }
    }
    bad
}

#[test]
fn api_row_004_no_row_past_r() {
    assert_eq!(row_004_past_r_facts(), Vec::<String>::new());
}

#[test]
fn api_row_006_flat_trips() {
    assert_eq!(
        row_006_facts(),
        vec![
            "store out[c1*8 + t1*8 : +8] <- p1",
            "store out[16 : +4] <- p2",
            "store out[20 : +1] <- p3",
        ]
    );
}

/// API.ROW.007's facts: `[start, trips]` of each core for 1, 5, 6 trips.
pub fn row_007_facts() -> Vec<Vec<(i64, i64)>> {
    let vc = card().vc.count as i64;
    [1, 5, 6]
        .into_iter()
        .map(|n| {
            let sp = v::split(n, vc);
            (0..sp.cores)
                .map(|c| {
                    let c = Idx::Int(c);
                    (sp.start(&c).int().unwrap(), sp.trips(&c).int().unwrap())
                })
                .collect()
        })
        .collect()
}

#[test]
fn api_row_007_two_core_split() {
    assert_eq!(
        row_007_facts(),
        vec![vec![(0, 1)], vec![(0, 3), (3, 2)], vec![(0, 3), (3, 3)]]
    );
}

#[test]
fn api_row_008_rows_of_other_widths_refused() {
    let e = mul2();
    let full = Operand::full(48, 8);
    let err = v::rows_kernel(&e, &[full], 8, 48, &full, None, &card()).unwrap_err();
    assert_eq!(err.clause, "API.ROW.008");
}

#[test]
fn api_row_009_row_output_needs_a_reduction() {
    let full = Operand::full(64, 8);
    let row = Operand::new(Kind::Row);
    let err = v::rows_kernel(&mul2(), &[full], 8, 64, &row, None, &card()).unwrap_err();
    assert_eq!(err.clause, "API.ROW.009");
    let two = expr::op(
        "add",
        vec![
            expr::reduce(RedOp::Sum, expr::load(0)).into(),
            expr::reduce(RedOp::Max, expr::load(0)).into(),
        ],
    )
    .unwrap();
    let wide = Operand::full(1024, 8);
    let err = v::rows_kernel(&two, &[wide], 8, 1024, &row, None, &card()).unwrap_err();
    assert_eq!(err.clause, "API.ROW.009");
}

#[test]
fn api_row_010_flat_one_value_operand() {
    let e = expr::op("mul", vec![expr::load(0).into(), expr::load(1).into()]).unwrap();
    let k = v::flat_kernel(
        &e,
        &[g::Dtype::F16; 2],
        3000,
        g::Dtype::F16,
        &[(1, 5)],
        &card(),
    )
    .unwrap();
    let in1 = k.graph.ports.iter().find(|p| p.name == "in1").unwrap();
    assert_eq!(in1.spec.shape, vec![card().vc.lanes as i64]);
    assert!(text(&k).contains("unpack xl2 lane=5"), "{}", text(&k));
}

/// API.FORM.010's facts: a statistic output's type and trip rows.
pub fn form_010_facts() -> (String, i64) {
    let e = expr::reduce(RedOp::Sum, expr::load(0));
    let row = Operand::new(Kind::Row);
    let (k, _) = v::rows_kernel(&e, &[Operand::full(64, 40)], 40, 64, &row, None, &card()).unwrap();
    let out = k.given.iter().find(|(n, _)| n == "out").unwrap();
    let hw = Hw::of(&card());
    let trip = v::plan(
        &[Operand::new(Kind::Full)],
        Kind::Row,
        40,
        64,
        None,
        None,
        16,
        &hw,
    )
    .unwrap()
    .rows;
    (ktpu_graph::text::spec(&out.1), trip)
}

#[test]
fn api_form_010_statistic_words() {
    assert_eq!(form_010_facts(), ("f16[48] @dram".to_string(), 16));
}

#[test]
fn api_read_001_operand_kinds() {
    let ops = [
        Operand::full(96, 50),
        Operand {
            offset: 32,
            ..Operand::new(Kind::Col)
        },
        Operand {
            offset: 16,
            rows: 64,
            ..Operand::new(Kind::Row)
        },
        Operand {
            offset: 5,
            rows: 16,
            ..Operand::new(Kind::Scalar)
        },
    ];
    let e = expr::op(
        "add",
        vec![
            expr::op("mul", vec![expr::load(0).into(), expr::load(1).into()])
                .unwrap()
                .into(),
            expr::op("add", vec![expr::load(2).into(), expr::load(3).into()])
                .unwrap()
                .into(),
        ],
    )
    .unwrap();
    let (k, _) = v::rows_kernel(&e, &ops, 37, 64, &Operand::full(96, 0), None, &card()).unwrap();
    let t = text(&k);
    // The col operand a [4, 64] row broadcast, the scalar's lane 5.
    let col = t
        .lines()
        .find(|l| l.contains("load in1[32 : +64]"))
        .expect(&t);
    assert!(col.ends_with(": f16[4, 64] @l1(gt4)"), "{col}");
    assert!(t.contains("lane=5"), "{t}");
}
