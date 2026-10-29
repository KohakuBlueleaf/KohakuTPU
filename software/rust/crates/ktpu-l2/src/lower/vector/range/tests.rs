//! Range facts and guard risks on whole instances.

use super::*;
use crate::ir::{Body, Program};
use crate::lower::vector::{ops_of, resolve};
use crate::mutant::{scoped, State};
use ktpu_l1::text::Env;

/// The vector instance of a `[16, 128]` stream loop running `lines` after
/// `v = unpack` of the loaded tile and before `out` is packed and stored.
fn instance(lines: &str) -> (Body, RPar) {
    let mut text = String::from(
        "target ktpu.v9\n\nfn k.l2(x: f16[16, 128] @dram, y: f16[16, 128] @dram)\n    \
         par c in 0..1 on vc[c]\n        for t in 0..1\n            \
         xl = load x[t*16 : +16, :]  : f16[16, 128] @l1\n            \
         v = unpack xl  : f32[16, 128] @reg\n",
    );
    for ln in lines.lines().filter(|l| !l.trim().is_empty()) {
        text.push_str(&format!("            {}\n", ln.trim_end()));
    }
    text.push_str(
        "            ol = pack out  : f16[16, 128] @l1\n            \
         store y[t*16 : +16, :] <- ol\n",
    );
    let body = Program::parse(&text, "<range>", None)
        .unwrap()
        .body()
        .unwrap();
    let par = body
        .stmts
        .iter()
        .find_map(|n| match n.as_ref() {
            Node::Par(p) => Some(p.clone()),
            _ => None,
        })
        .unwrap();
    let rp = resolve(&body, &par, &Env::new()).unwrap();
    (body, rp)
}

/// Each EXP2 / EXP2D op's target and risk under `ranges`, in program order.
fn risks(lines: &str, ranges: Ranges) -> Vec<(String, Risk)> {
    let (_, rp) = instance(lines);
    let g = guards(&rp, ranges);
    let mut out = vec![];
    for n in &rp.body {
        let nodes = match n.as_ref() {
            Node::For(f) => ops_of(&f.body),
            _ => vec![n.clone()],
        };
        for o in nodes {
            let Node::Op(o) = o.as_ref() else { continue };
            if o.op == "exp2" || o.op == "exp2d" {
                let r = g.get(&o.uid).copied().unwrap_or(Risk::ALL);
                out.push((o.target.clone().unwrap(), r));
            }
        }
    }
    out
}

fn risk_of(lines: &str, ranges: Ranges, name: &str) -> Risk {
    risks(lines, ranges)
        .into_iter()
        .find(|(n, _)| n == name)
        .unwrap_or_else(|| panic!("no exp2 into {name}"))
        .1
}

const LOW: Risk = Risk {
    high: false,
    low: true,
};
const HIGH: Risk = Risk {
    high: true,
    low: false,
};

const ROW: &str = ": f32[16, 128] @reg";
const STAT: &str = ": f32[16] @reg";

fn rows(ops: &[&str]) -> String {
    ops.iter()
        .map(|o| {
            let ty = if o.contains(" rmax ") || o.contains(" rsum ") {
                STAT
            } else {
                ROW
            };
            format!("{o}  {ty}\n")
        })
        .collect()
}

#[test]
fn the_risk_thresholds_are_the_s8_17_domain_per_op() {
    let f = |lo, hi| Fact::of(lo, hi, true);
    assert_eq!(risk("exp2", f(-128.0, 127.99)), Risk::NONE);
    assert_eq!(risk("exp2", f(-128.01, 127.99)), LOW);
    assert_eq!(risk("exp2", f(-128.0, 128.0)), HIGH);
    // EXP2D's designed special starts at -128 itself.
    assert_eq!(risk("exp2d", f(-127.99, 127.99)), Risk::NONE);
    assert_eq!(risk("exp2d", f(-128.0, 127.99)), LOW);
    assert_eq!(risk("exp2d", f(-127.99, 128.0)), HIGH);
    assert_eq!(risk("exp2", Fact::TOP), Risk::ALL);
}

#[test]
fn loads_are_unbounded_and_clamps_bound() {
    let src = rows(&[
        "a = exp2 v",
        "b = min v, 100.0",
        "c = exp2 b",
        "d = max b, -100.0",
        "e = exp2 d",
        "f = max v, -100.0",
        "g = exp2 f",
        "h = exp2d d, v",
        "out = mov e",
    ]);
    assert_eq!(risk_of(&src, Ranges::Intervals, "a"), Risk::ALL);
    assert_eq!(risk_of(&src, Ranges::Intervals, "c"), LOW);
    assert_eq!(risk_of(&src, Ranges::Intervals, "e"), Risk::NONE);
    assert_eq!(risk_of(&src, Ranges::Intervals, "g"), HIGH);
    assert_eq!(risk_of(&src, Ranges::Intervals, "h"), Risk::NONE);
    // Bounds a whisker inside the domain stay proven; at its edge they do not.
    let edge = rows(&[
        "b = min v, 127.9",
        "d = max b, -127.9",
        "e = exp2 d",
        "f = min v, 128.0",
        "g = max f, -127.9",
        "h = exp2 g",
        "out = mov e",
    ]);
    assert_eq!(risk_of(&edge, Ranges::Intervals, "e"), Risk::NONE);
    assert_eq!(risk_of(&edge, Ranges::Intervals, "h"), HIGH);
}

#[test]
fn off_proves_nothing() {
    let src = rows(&["b = min v, 100.0", "d = max b, -100.0", "out = exp2 d"]);
    assert_eq!(risk_of(&src, Ranges::Intervals, "out"), Risk::NONE);
    assert_eq!(risk_of(&src, Ranges::Off, "out"), Risk::ALL);
    let (_, rp) = instance(&src);
    assert!(guards(&rp, Ranges::Off).is_empty());
    #[cfg(feature = "mutants")]
    assert!(mutated(OFF_MUTANT, || guards(&rp, Ranges::RowMax)).is_empty());
}

#[test]
fn bounded_compositions() {
    // tanh(y) = 2 sigmoid(2y) - 1 in [-1, 1]; 8 tanh(y) in [-8, 8].
    let tanh = rows(&[
        "a = mul v, -2.8853900817779268",
        "b = exp2 a",
        "c = add b, 1.0",
        "d = inv c",
        "e = fma d, 2.0, -1.0",
        "f = mul e, 8.0",
        "out = exp2 f",
    ]);
    assert_eq!(risk_of(&tanh, Ranges::Intervals, "b"), Risk::ALL);
    assert_eq!(risk_of(&tanh, Ranges::Intervals, "out"), Risk::NONE);
    // The same with the scale past the domain.
    let wide = tanh.replace("mul e, 8.0", "mul e, 200.0");
    assert_eq!(risk_of(&wide, Ranges::Intervals, "out"), Risk::ALL);
    // A compare is 0 or 1, a select one of its arms.
    let sel = rows(&[
        "m = cmplt v, 0.0",
        "s = sel m, 3.0, -3.0",
        "a = exp2 s",
        "n = fnma m, 300.0, 1.0",
        "b = exp2 n",
        "out = mov a",
    ]);
    assert_eq!(risk_of(&sel, Ranges::Intervals, "a"), Risk::NONE);
    assert_eq!(risk_of(&sel, Ranges::Intervals, "b"), LOW);
    // A row sum of [0, 1] over 128 columns reaches 128; half of it does not.
    let sum = rows(&[
        "m = cmplt v, 0.0",
        "s = rsum m",
        "a = exp2 s",
        "h = mul s, 0.5",
        "b = exp2 h",
        "out = mov v",
    ]);
    assert_eq!(risk_of(&sum, Ranges::Intervals, "a"), HIGH);
    assert_eq!(risk_of(&sum, Ranges::Intervals, "b"), Risk::NONE);
}

#[test]
fn a_rebound_name_takes_its_new_fact() {
    let src = rows(&[
        "a = min v, 10.0",
        "a = max a, -10.0",
        "b = exp2 a",
        "a = mul a, 100.0",
        "c = exp2 a",
        "out = mov b",
    ]);
    assert_eq!(risk_of(&src, Ranges::Intervals, "b"), Risk::NONE);
    assert_eq!(risk_of(&src, Ranges::Intervals, "c"), Risk::ALL);
}

/// `a` bounded, then rebound only inside `if sticky`: after it, either.
const STICKY: &str = "a = mul v, 1000.0  : f32[16, 128] @reg
if sticky
    b = min v, 10.0  : f32[16, 128] @reg
    a = max b, -10.0  : f32[16, 128] @reg
out = exp2 a  : f32[16, 128] @reg
";

#[test]
fn a_sticky_block_binds_either_value() {
    assert_eq!(risk_of(STICKY, Ranges::Intervals, "out"), Risk::ALL);
}

#[test]
fn the_stream_loop_entry_forgets_what_the_loop_assigns() {
    // `a` bounded before the loop (its two clamps moved ahead of it); the
    // loop's first read of `a` is an earlier trip's value, unbounded.
    let (_, mut rp) = instance(&rows(&["p = exp2 a", "a = mul v, 1000.0", "out = mov p"]));
    let (_, pre) = instance(&rows(&[
        "a = min v, 10.0",
        "a = max a, -10.0",
        "out = mov a",
    ]));
    let Node::For(f) = pre.body[0].as_ref() else {
        panic!("a stream loop")
    };
    rp.body.splice(0..0, f.body[2..4].to_vec());
    let Node::For(lp) = rp.body.last().unwrap().as_ref() else {
        panic!("a stream loop")
    };
    let p = lp
        .body
        .iter()
        .find_map(|n| match n.as_ref() {
            Node::Op(o) if o.op == "exp2" => Some(o.uid),
            _ => None,
        })
        .unwrap();
    assert_eq!(guards(&rp, Ranges::Intervals)[&p], Risk::ALL);
    #[cfg(feature = "mutants")]
    {
        let stale = mutated(STALE_MUTANT, || guards(&rp, Ranges::Intervals)[&p]);
        assert_eq!(stale, Risk::NONE);
    }
}

/// A softmax body: `e - rmax e` into EXP2, normalised by its row sum.
const SOFTMAX: &[&str] = &[
    "e = mul v, 0.7213475204444817",
    "s = rmax e",
    "d = sub e, s[:, *]",
    "p = exp2 d",
    "l = rsum p",
    "r = inv l",
    "out = mul p, r[:, *]",
];

/// `SOFTMAX` with `lines` in place of its last.
fn softmax_ending(lines: &[&str]) -> String {
    let mut ops = SOFTMAX[..SOFTMAX.len() - 1].to_vec();
    ops.extend(lines);
    rows(&ops)
}

#[test]
fn rowmax_bounds_x_minus_its_row_max_above() {
    let src = rows(SOFTMAX);
    assert_eq!(risk_of(&src, Ranges::Intervals, "p"), Risk::ALL);
    assert_eq!(risk_of(&src, Ranges::RowMax, "p"), LOW);
    // Scaled after the subtraction, by a positive factor.
    let scaled = rows(&[
        "s = rmax v",
        "d = sub v, s[:, *]",
        "e = mul d, 1.4426950408889634",
        "p = exp2 e",
        "l = rsum p",
        "out = mul p, l[:, *]",
    ]);
    assert_eq!(risk_of(&scaled, Ranges::RowMax, "p"), LOW);
    // The maximum of another binding of `e`.
    let rebound = rows(
        &SOFTMAX
            .join("\n")
            .replacen("s = rmax e\n", "s = rmax e\ne = mul e, 2.0\n", 1)
            .lines()
            .collect::<Vec<_>>(),
    );
    assert_eq!(risk_of(&rebound, Ranges::RowMax, "p"), Risk::ALL);
    // The maximum of other columns.
    let sliced = rows(
        &SOFTMAX
            .join("\n")
            .replace("rmax e", "rmax e[:, 0:64]")
            .lines()
            .collect::<Vec<_>>(),
    );
    assert_eq!(risk_of(&sliced, Ranges::RowMax, "p"), Risk::ALL);
    // The maximum subtracted the other way.
    let reversed = rows(
        &SOFTMAX
            .join("\n")
            .replace("sub e, s[:, *]", "sub s[:, *], e")
            .lines()
            .collect::<Vec<_>>(),
    );
    assert_eq!(risk_of(&reversed, Ranges::RowMax, "p"), Risk::ALL);
}

#[test]
fn rowmax_holds_only_where_every_output_is_nan_across_a_nan_row() {
    // The row sum reads every lane, a NaN one included: a value scaled by its
    // reciprocal is NaN across the row, and so is a sum with that value.
    let with_sum = softmax_ending(&["q = mul p, r[:, *]", "out = add q, p"]);
    assert_eq!(risk_of(&with_sum, Ranges::RowMax, "p"), LOW);
    // `p` itself stored.
    for tail in [
        &["out = mov p"][..],
        &["out = mul p, 2.0"],
        // `vmax` keeps a non-NaN first operand past a NaN second.
        &["out = max p, l[:, *]"],
        // A sum of half the columns misses a NaN in the other half.
        &["h = rsum p[:, 0:64]", "out = mul p, h[:, *]"],
    ] {
        let src = softmax_ending(tail);
        assert_eq!(risk_of(&src, Ranges::RowMax, "p"), Risk::ALL, "{tail:?}");
        #[cfg(feature = "mutants")]
        assert_eq!(
            mutated(UNPOISONED_MUTANT, || risk_of(&src, Ranges::RowMax, "p")),
            LOW,
            "{tail:?}"
        );
    }
}

#[test]
fn rowmax_over_masked_columns_bounds_nothing() {
    let src = rows(SOFTMAX).replace("s = rmax e ", "s = rmax e valid_cols=64 ");
    assert!(src.contains("valid_cols=64"));
    assert_eq!(risk_of(&src, Ranges::RowMax, "p"), Risk::ALL);
    #[cfg(feature = "mutants")]
    assert_eq!(
        mutated(MASKED_MUTANT, || risk_of(&src, Ranges::RowMax, "p")),
        LOW
    );
}

#[cfg(feature = "mutants")]
#[test]
fn the_floor_mutant_guards_every_exp2_by_its_floor() {
    let src = rows(&["a = exp2 v", "out = exp2d a, v"]);
    let got = mutated(FLOOR_MUTANT, || risks(&src, Ranges::Intervals));
    assert_eq!(got[0], ("a".to_string(), LOW));
    assert_eq!(got[1], ("out".to_string(), HIGH));
}

/// `c = max a, y` with `a` in [-200, -150] and `y` at least 50 or NaN:
/// a NaN `y` gives `a`, so `c` reaches -200.
const NAN_SECOND: &[&str] = &[
    "a = min v, -150.0",
    "a = max a, -200.0",
    "y = max v, 50.0",
    "c = max a, y",
    "d = min c, 100.0",
    "out = exp2 d",
];

#[test]
fn max_and_min_keep_their_first_operand_past_a_nan() {
    let src = rows(NAN_SECOND);
    assert_eq!(risk_of(&src, Ranges::Intervals, "out"), LOW);
    // `min` likewise: `y` at most -50 or NaN, `a` in [150, 200].
    let min = rows(&[
        "a = min v, 200.0",
        "a = max a, 150.0",
        "y = min v, -50.0",
        "c = min a, y",
        "d = max c, -100.0",
        "out = exp2 d",
    ]);
    assert_eq!(risk_of(&min, Ranges::Intervals, "out"), HIGH);
    #[cfg(feature = "mutants")]
    {
        let blind = |s: &str| mutated(NAN_BLIND_MUTANT, || risk_of(s, Ranges::Intervals, "out"));
        assert_eq!(blind(&src), Risk::NONE);
        assert_eq!(blind(&min), Risk::NONE);
    }
}

#[cfg(feature = "mutants")]
fn mutated<T>(m: &'static str, f: impl FnOnce() -> T) -> T {
    scoped(
        State {
            active: Some(m),
            ..State::default()
        },
        f,
    )
}

#[cfg(feature = "mutants")]
#[test]
fn the_assumed_mutant_proves_every_argument() {
    let src = rows(&["a = exp2 v", "out = exp2d a, v"]);
    let all = mutated(ASSUMED_MUTANT, || risks(&src, Ranges::Off));
    assert_eq!(all.len(), 2);
    assert!(all.iter().all(|(_, r)| *r == Risk::NONE));
    // Unmutated, `a` is unbounded and `2^a` is at least 0, below unbounded.
    assert_eq!(
        risks(&src, Ranges::Intervals),
        [("a".to_string(), Risk::ALL), ("out".to_string(), HIGH)]
    );
}

#[cfg(feature = "mutants")]
#[test]
fn the_stale_mutant_carries_a_sticky_block_out_unjoined() {
    let got = mutated(STALE_MUTANT, || risk_of(STICKY, Ranges::Intervals, "out"));
    assert_eq!(got, Risk::NONE);
}
