//! `unary.pest` reads every golden text as `unary_v0.pest` does: each line of
//! each L1 and L2 text in `golden/{l1,l2,e2e}` read by both grammars, the
//! statements and stop positions equal (`text::parse::read_both`).

#![cfg(feature = "mutants")]

use std::path::Path;

use serde_json::Value;

/// Every string under a source key of `v`.
fn texts<'a>(v: &'a Value, out: &mut Vec<&'a str>) {
    match v {
        Value::Object(m) => {
            for (k, x) in m {
                match x {
                    Value::String(s) if ["l1", "l2", "text", "source"].contains(&k.as_str()) => {
                        out.push(s)
                    }
                    _ => texts(x, out),
                }
            }
        }
        Value::Array(a) => a.iter().for_each(|x| texts(x, out)),
        _ => {}
    }
}

#[test]
fn both_grammars_read_every_golden_text_alike() {
    ktpu_l1::text::parse::read_both();
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden");
    let (mut files, mut lines) = (0, 0);
    for sub in ["l1", "l2", "e2e"] {
        for e in std::fs::read_dir(root.join(sub)).unwrap().flatten() {
            let p = e.path();
            if p.extension().is_none_or(|x| x != "json") {
                continue;
            }
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            let mut ts = Vec::new();
            texts(&v, &mut ts);
            for t in ts {
                lines += t.lines().count();
                let _ = ktpu_l1::text::parse(t, "<golden>");
            }
            files += 1;
        }
    }
    println!("{files} files, {lines} lines read by both grammars");
    assert!(
        files > 500 && lines > 100_000,
        "{files} files, {lines} lines"
    );
}

#[test]
fn a_placed_term_reads_alike() {
    ktpu_l1::text::parse::read_both();
    for t in [
        "x = op a @l1, b[0 : +4] @A, -c @l1(gt4), (d @acc)\n",
        "y = fill w[k*64 : +64] : mx7b[256, 64] @B\n",
        "op a @\n",
        "op @l1\n",
        "op (1, 2\n",
    ] {
        let _ = ktpu_l1::text::parse(t, "<t>");
    }
}
