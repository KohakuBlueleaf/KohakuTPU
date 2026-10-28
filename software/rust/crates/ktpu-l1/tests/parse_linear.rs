//! The grammar reads a line in calls linear in its nesting: a 24-deep view
//! parses within a call budget the twice-read `placed` rule
//! (`l1.text/placed-first`, 2^24 atom reads) exceeds. Its own binary: the
//! budget is pest's process-wide call limit.

use std::num::NonZeroUsize;

use ktpu_l1::text::parse;

const DEPTH: usize = 24;
/// Pest rule calls one line may take: about 40 a level here.
const BUDGET: usize = 4000;

fn nested() -> String {
    let mut t = "0".to_string();
    for _ in 0..DEPTH {
        t = format!("x[{t}]");
    }
    format!("op {t}, y @l1\n")
}

#[test]
fn nested_views_parse_within_the_budget() {
    pest::set_call_limit(NonZeroUsize::new(BUDGET));
    let got = parse(&nested(), "<nested>");
    assert!(got.is_ok(), "{:?}", got.err());
    assert_eq!(got.unwrap()[0].args.len(), 2);
}

#[cfg(feature = "mutants")]
#[test]
fn placed_first_reads_past_the_budget() {
    pest::set_call_limit(NonZeroUsize::new(BUDGET));
    let got = ktpu_l1::mutant::with("l1.text/placed-first", || parse(&nested(), "<nested>"));
    assert!(got.is_err(), "the mutant parsed within {BUDGET} calls");
}
