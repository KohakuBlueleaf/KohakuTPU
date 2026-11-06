use std::collections::BTreeMap;

use ktpu_l0::format::Cond;
use ktpu_l0::program::Stmt as NodeStmt;
use ktpu_l1::build::programs;
use ktpu_l1::text::read;

use super::common::card;

fn branch(cond: Cond, a: u64, b: u64) -> bool {
    match cond {
        Cond::Always => true,
        Cond::Eq => a == b,
        Cond::Ne => a != b,
        Cond::Ltu => a < b,
        Cond::Geu => a >= b,
        Cond::Lt => (a as i64) < (b as i64),
        Cond::Ge => (a as i64) >= (b as i64),
    }
}

#[test]
fn immediate_ordering_at_domain_boundaries() {
    let machine = card();
    for signed in [false, true] {
        let max = if signed { i64::MAX as u64 } else { u64::MAX };
        for literal in [i128::from(max - 1), i128::from(max), i128::MAX] {
            let k = literal as u64;
            for op in [">", "<="] {
                let suffix = if signed { " signed" } else { "" };
                let source = format!(
                    "fn f.l1()\n    goto end if r0 {op} {literal}{suffix}\n    label end\n"
                );
                let module = read(&source, "branch").expect("L1");
                let lowered = programs(&module, "f", &BTreeMap::new(), &machine, Some(1), None)
                    .expect("lowering");
                for a in [0, max - 1, max, 1u64 << 63] {
                    let taken = lowered[0].program.steps.iter().any(|s| match s {
                        NodeStmt::Jump { cond, b, .. } => branch(*cond, a, *b),
                        _ => false,
                    });
                    let greater = if signed {
                        (a as i64) > (k as i64)
                    } else {
                        a > k
                    };
                    assert_eq!(taken, if op == ">" { greater } else { !greater });
                }
            }
        }
    }
}
