use ktpu_l1::text::Stmt;
use ktpu_l2::ir::{instances, Program, StageX};
use ktpu_l2::lower::vector;
use ktpu_l2::passes::retile::{retile_profiled, scaled};
use ktpu_l2::passes::{body_of, write};
use ktpu_l2::target::Target;
use ktpu_l2::Search;

use super::common::card;

// Preserve the written-first traversal and the strict improvement threshold.
fn exhaustive(p: &Program, target: &Target) -> Stmt {
    let (params, stmts) = body_of(p).expect("body");
    let search = Search::default();
    let mut best = None;
    let mut cost = None;
    for rows in [16, 4, 8, 32, 64] {
        let mut candidate = stmts[0].clone();
        candidate.body[0] = scaled(&candidate.body[0], "t", 16, rows);
        let trial = write(p, &params, &[candidate.clone()]).expect("candidate");
        let body = trial.body().expect("candidate body");
        let mut total = 0;
        let mut legal = true;
        for stage in instances(&body).expect("instances") {
            if let StageX::Par(parts) = stage.x {
                let mut longest = 0;
                for part in parts {
                    match vector::total(&body, part.par(), &part.env, target, &search) {
                        Ok(cycles) => longest = longest.max(cycles),
                        Err(_) => legal = false,
                    }
                }
                total += longest;
            }
        }
        if legal && cost.is_none_or(|old: i64| (total as f64) < old as f64 * 0.98) {
            best = Some(candidate);
            cost = Some(total);
        }
    }
    best.expect("at least one tile lowers")
}

#[test]
fn repeated_row_shares_price_once_and_keep_the_exhaustive_choice() {
    let header = "target ktpu.v9\nfn rows.l2(x: f16[256, 32] @dram, y: f16[256, 32] @dram)\n";
    let share = r#"    par c in 0..1 on vc[c]
        for t in 0..16
            h = load x[t*16:+16, 0:32] : f16[16, 32] @l1(gt4)
            v = unpack h : f32[16, 32] @reg(gt4)
            z = mul v, 0.5 : f32[16, 32] @reg(gt4)
            q = pack z : f16[16, 32] @l1(gt4)
            store y[t*16:+16, 0:32] <- q
"#;
    let one =
        Program::parse(&format!("{header}{share}"), "rows", Some("rows")).expect("one row share");
    let target = Target::of(&card());
    let expected = exhaustive(&one, &target);
    let (_, single_prices) =
        retile_profiled(&one, &target, &Search::default()).expect("single share retile");
    let repeated = Program::parse(
        &format!("{header}{}", share.repeat(4)),
        "rows",
        Some("rows"),
    )
    .expect("repeated row shares");
    let search = Search::default();
    let (tiled, prices) =
        retile_profiled(&repeated, &target, &search).expect("repeated share retile");
    assert_eq!(prices, single_prices, "equal shares reuse candidate prices");
    assert!(prices > 1, "the witness exercises competing row tiles");
    let (_, result) = body_of(&tiled).expect("retiled body");
    for (index, actual) in result.iter().enumerate() {
        // Canonical printing ignores the parser's source positions.
        assert_eq!(
            ktpu_l1::text::emit(&[actual.clone()]),
            ktpu_l1::text::emit(&[expected.clone()]),
            "share {index} keeps the exhaustive winner"
        );
    }
}
